"""Side questions (``/btw``): a quick answer from a temporary copy of a chat.

The copy is an ephemeral fork of the chat's own provider session
(``scripts/fork_session.py``) with every tool turned off, so it can only
answer, and the provider never saves it. Asking never joins the chat's queue
or interrupts a running turn. Nothing reaches the transcript unless the owner
adds the answer (``chat_writer.AppendSideQuestion``); an added answer is a
``side_question`` row that the agent receives once, at the start of its next
successful turn (``owed_side_questions`` and the provider-success ack).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app import models

log = logging.getLogger(__name__)

# A fork rereads the whole session before answering, so a long chat takes a
# while; past this the owner is better served by an error than a spinner.
SIDE_QUESTION_TIMEOUT_SECS = 180
MAX_QUESTION_CHARS = 4000
# An added answer becomes part of the agent's next turn, so keep it bounded.
MAX_ANSWER_CHARS = 20000
FORKABLE_PROVIDERS = ("claude", "codex")

_FORK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fork_session.py"

# One side question at a time per chat. Process-local on purpose: the platform
# serves chats from one backend process, and a restart ends any fork anyway.
_ANSWERING: set[str] = set()

_PROMPT = (
  "The owner is asking a quick side question while the main work continues. "
  "You are a temporary copy of this conversation with no tools. Answer the "
  "question directly and briefly from what this conversation already shows. "
  "Do not continue, restart, or plan the main task, and do not ask the owner "
  "to confirm anything.\n\n"
  "Side question: {question}"
)


class SideQuestionError(Exception):
  """A side question that cannot be answered, with an owner-facing reason."""

  def __init__(self, status_code: int, message: str):
    super().__init__(message)
    self.status_code = status_code
    self.message = message


@dataclass(frozen=True)
class SideQuestionPlan:
  """Everything a fork needs, read from the chat before any waiting starts."""

  chat_id: str
  provider: str
  session_id: str
  model: str | None
  question: str


@dataclass(frozen=True)
class SideAnswer:
  question: str
  answer: str
  provider: str


def _forkable_session(db: Session, chat: models.Chat, provider: str) -> str | None:
  """The chat's current provider session, else its latest same-provider one.

  A settled chat can clear ``session_id`` while its append-only session link
  stays exact (the same rule Agent Coaching's fork_chat.py uses). Sessions of
  another provider are never borrowed.
  """
  if chat.session_id:
    return chat.session_id
  row = (
    db.query(models.ChatSessionLink.session_id)
    .filter(
      models.ChatSessionLink.chat_id == chat.id,
      models.ChatSessionLink.provider == provider,
    )
    .order_by(
      models.ChatSessionLink.last_seen_at.desc(),
      models.ChatSessionLink.session_id.desc(),
    )
    .first()
  )
  return row[0] if row else None


def _chat_model(chat: models.Chat) -> str | None:
  settings = chat.agent_settings_json if isinstance(chat.agent_settings_json, dict) else {}
  model = settings.get("model")
  return model.strip() if isinstance(model, str) and model.strip() else None


def clean_question(question: str) -> str:
  text = (question or "").strip()
  if not text:
    raise SideQuestionError(422, "Type a question after /btw.")
  if len(text) > MAX_QUESTION_CHARS:
    raise SideQuestionError(
      422, f"Side questions can be up to {MAX_QUESTION_CHARS} characters.",
    )
  return text


def plan_side_question(
  db: Session, chat: models.Chat, question: str,
) -> SideQuestionPlan:
  """Validate the question and pick the exact session the copy forks."""
  question = clean_question(question)
  provider = (chat.provider or "claude").strip().lower()
  if provider not in FORKABLE_PROVIDERS:
    raise SideQuestionError(422, "Side questions work in Claude and Codex chats.")
  session_id = _forkable_session(db, chat, provider)
  if not session_id:
    raise SideQuestionError(
      409,
      "This chat has nothing to answer from yet. Ask again after its first reply.",
    )
  return SideQuestionPlan(
    chat_id=chat.id,
    provider=provider,
    session_id=session_id,
    model=_chat_model(chat),
    question=question,
  )


async def answer_side_question(
  plan: SideQuestionPlan,
  data_dir: str | Path,
  *,
  timeout_secs: float = SIDE_QUESTION_TIMEOUT_SECS,
) -> SideAnswer:
  """Run one ephemeral, tool-less fork and return its answer."""
  from app.providers import get_provider

  if plan.chat_id in _ANSWERING:
    raise SideQuestionError(
      409, "A side question is already being answered in this chat.",
    )
  env = get_provider(plan.provider).build_env(
    base_env=dict(os.environ), data_dir=str(data_dir),
  )
  command = [
    sys.executable, str(_FORK_SCRIPT), "--json", "--ephemeral",
    *(["--model", plan.model] if plan.model else []),
    plan.provider, plan.session_id, str(data_dir),
    _PROMPT.format(question=plan.question),
  ]
  _ANSWERING.add(plan.chat_id)
  try:
    # The chat's own session lives under its working directory (/data), so
    # the fork must start there; with no tools it cannot touch any file.
    proc = await asyncio.create_subprocess_exec(
      *command,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      env=env,
      cwd=str(data_dir),
      start_new_session=True,
    )
    try:
      stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_secs)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
      # The provider CLI runs in the fork's own process group: end all of it.
      try:
        os.killpg(proc.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      await proc.communicate()
      if isinstance(exc, asyncio.CancelledError):
        raise
      raise SideQuestionError(
        504, "The side question took too long to answer. Try again.",
      ) from exc
  finally:
    _ANSWERING.discard(plan.chat_id)
  if proc.returncode:
    detail = (stderr or b"").decode("utf-8", "replace").strip()
    raise SideQuestionError(502, _fork_failure_message(detail))
  try:
    payload = json.loads(stdout.decode("utf-8"))
    answer = str(payload["answer"]).strip()
  except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
    raise SideQuestionError(502, "The side question came back unreadable.") from exc
  if not answer:
    raise SideQuestionError(502, "The side question came back empty.")
  return SideAnswer(question=plan.question, answer=answer, provider=plan.provider)


def _fork_failure_message(detail: str) -> str:
  """An owner-facing reason; the script's own words stay in the server log."""
  log.warning("side question fork failed: %s", detail[-2000:])
  if "not found" in detail or "expired" in detail:
    return "This chat's session can't be copied right now, so the side question couldn't run."
  return "The side question couldn't be answered. Try again."


def side_question_row_content(question: str, answer: str) -> str:
  """The text an added side question carries for the agent and for search."""
  return f"Side question: {question}\n\nAnswer (from a temporary copy of this chat): {answer}"


def owed_side_questions_context(rows: list[dict]) -> str:
  """The block a turn carries for side questions the agent hasn't seen yet."""
  if not rows:
    return ""
  body = "\n\n".join(
    side_question_row_content(
      str((row.get("side_question") or {}).get("question") or ""),
      str((row.get("side_question") or {}).get("answer") or ""),
    )
    for row in rows
  )
  return (
    "The owner asked side questions that a temporary copy of this chat "
    "answered, and added them to the chat so you know about them. They are "
    "context, not a new request.\n\n"
    f"<side_questions>\n{body}\n</side_questions>"
  )
