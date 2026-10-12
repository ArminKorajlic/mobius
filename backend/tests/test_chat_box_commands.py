"""The composer's /context and /btw chat commands (and the transcript row /btw adds).

/context reads estimated sizes of what Möbius adds to a chat's context; /btw
asks a temporary, never-saved copy of the chat; Add to chat stores one
``side_question`` row that the agent receives once, on its next successful
turn.
"""

import asyncio
import json
import sys
import textwrap
import time

import pytest

from app import chat as chat_mod
from app import chat_queue, models, schemas, side_questions, transcript_rows
from app.agent_context import AgentContextParts, context_part_sizes, estimate_tokens
from app.broadcast import create_broadcast, remove_broadcast
from app.chat_writer import (
  AcknowledgeProviderSuccess,
  AdmitProviderExecution,
  AppendSideQuestion,
  StartTurn,
  get_writer,
)
from app.database import SessionLocal
from app.session_links import record_session_link


def _keep(chat_id, cid="side-1", question="what runs?", answer="The Kanban tests."):
  return get_writer().submit(AppendSideQuestion(
    chat_id=chat_id, cid=cid, question=question, answer=answer, provider="claude",
  )).result(timeout=5)


def _rows(chat_id):
  with SessionLocal() as db:
    return transcript_rows.read_all(db, db.get(models.Chat, chat_id))


def _owed(chat_id):
  with SessionLocal() as db:
    return transcript_rows.owed_side_questions(db, db.get(models.Chat, chat_id))


# ── /context ─────────────────────────────────────────────────────────────


def test_part_sizes_estimate_four_characters_per_token_and_report_absent_parts_as_zero():
  parts = AgentContextParts(
    system_prompt="x" * 400,
    system_prompt_source="skill_preview",
    system_prompt_origin="platform",
    skills_list="y" * 41,
    recent_chats=None,
    app_context="a" * 100,
    app_report="r" * 98,
    compaction_brief=None,
  )

  assert context_part_sizes(parts) == [
    {"key": "instructions", "tokens": 100},
    {"key": "skills", "tokens": 11},
    {"key": "recent_chats", "tokens": 0},
    # The report travels with the app context, so they are one part.
    {"key": "app_context", "tokens": estimate_tokens("a" * 100 + "\n\n" + "r" * 98)},
    {"key": "compaction", "tokens": 0},
  ]


def test_context_sizes_route_returns_every_part_size_but_no_text(client, auth, chat):
  response = client.get(f"/api/chats/{chat.id}/context-sizes", headers=auth)

  assert response.status_code == 200, response.text
  payload = response.json()
  assert list(payload) == ["parts"]
  assert [part["key"] for part in payload["parts"]] == [
    "instructions", "skills", "recent_chats", "app_context", "compaction",
  ]
  assert all(isinstance(part["tokens"], int) and part["tokens"] >= 0 for part in payload["parts"])
  # The constitution is always there, so the instructions are never empty.
  assert payload["parts"][0]["tokens"] > 0


def test_context_sizes_route_is_owner_only(client, chat):
  assert client.get(f"/api/chats/{chat.id}/context-sizes").status_code == 401


# ── /btw: asking ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_fork(tmp_path, monkeypatch):
  """Replace fork_session.py with a script that records its argv and answers."""
  argv_file = tmp_path / "argv.json"

  def install(body: str):
    script = tmp_path / "fake_fork.py"
    script.write_text(textwrap.dedent(f"""
      import json, sys, time
      with open({str(argv_file)!r}, "w") as handle:
        json.dump(sys.argv[1:], handle)
      {body}
    """))
    monkeypatch.setattr(side_questions, "_FORK_SCRIPT", script)
    return argv_file

  return install


def _with_session(db, chat, *, provider="claude", session_id="source-session"):
  chat.provider = provider
  chat.session_id = session_id
  db.commit()


def test_side_question_answers_from_an_ephemeral_fork_on_the_chats_model(
  client, auth, chat, db, fake_fork,
):
  argv_file = fake_fork('print(json.dumps({"answer": "The Kanban tests."}))')
  _with_session(db, chat)

  response = client.post(
    f"/api/chats/{chat.id}/btw", headers=auth, json={"question": "what runs?"},
  )

  assert response.status_code == 200, response.text
  assert response.json() == {
    "question": "what runs?", "answer": "The Kanban tests.", "provider": "claude",
  }
  argv = json.loads(argv_file.read_text())
  assert argv[:3] == ["--json", "--ephemeral", "--model"]
  assert argv[3] == "claude-opus-4-8"
  assert argv[4:7] == ["claude", "source-session", argv[6]]
  assert "Side question: what runs?" in argv[-1]
  # Asking saves nothing: the transcript is exactly as it was.
  assert _rows(chat.id) == []


def test_side_question_before_the_first_reply_says_there_is_nothing_to_answer_from(
  client, auth, chat,
):
  response = client.post(
    f"/api/chats/{chat.id}/btw", headers=auth, json={"question": "what runs?"},
  )

  assert response.status_code == 409
  assert "nothing to answer from yet" in response.json()["detail"]


def test_side_question_uses_the_chats_latest_same_provider_session(db, chat):
  chat.provider = "claude"
  chat.session_id = None
  db.commit()
  record_session_link(db, "codex", "other-provider-thread", chat.id)
  record_session_link(db, "claude", "settled-session", chat.id)

  plan = side_questions.plan_side_question(db, chat, "what runs?")

  assert plan.session_id == "settled-session"


def test_side_questions_need_a_forkable_provider(db, chat):
  _with_session(db, chat, provider="mobius")

  with pytest.raises(side_questions.SideQuestionError) as refused:
    side_questions.plan_side_question(db, chat, "what runs?")

  assert refused.value.status_code == 422
  assert "Claude and Codex" in refused.value.message


@pytest.mark.parametrize("question", ["", "   ", "x" * 4001])
def test_side_question_text_is_required_and_bounded(client, auth, chat, db, question):
  _with_session(db, chat)
  response = client.post(
    f"/api/chats/{chat.id}/btw", headers=auth, json={"question": question},
  )
  assert response.status_code == 422


def test_a_side_question_that_runs_too_long_is_stopped_and_reported(db, chat, fake_fork):
  fake_fork("time.sleep(30)")
  _with_session(db, chat)
  plan = side_questions.plan_side_question(db, chat, "what runs?")

  started = time.monotonic()
  with pytest.raises(side_questions.SideQuestionError) as stopped:
    asyncio.run(side_questions.answer_side_question(plan, "/tmp", timeout_secs=0.5))

  assert stopped.value.status_code == 504
  assert time.monotonic() - started < 10
  # The chat is free for its next side question.
  assert chat.id not in side_questions._ANSWERING


def test_only_one_side_question_runs_per_chat(db, chat, fake_fork):
  fake_fork('time.sleep(1); print(json.dumps({"answer": "done"}))')
  _with_session(db, chat)
  plan = side_questions.plan_side_question(db, chat, "what runs?")

  async def both():
    first = asyncio.create_task(side_questions.answer_side_question(plan, "/tmp"))
    await asyncio.sleep(0.2)
    with pytest.raises(side_questions.SideQuestionError) as busy:
      await side_questions.answer_side_question(plan, "/tmp")
    return busy.value, await first

  busy, answer = asyncio.run(both())
  assert busy.status_code == 409
  assert answer.answer == "done"


def test_a_failed_fork_gives_a_plain_reason(db, chat, fake_fork):
  fake_fork('sys.stderr.write("fork-session: Claude session transcript not found"); sys.exit(1)')
  _with_session(db, chat)
  plan = side_questions.plan_side_question(db, chat, "what runs?")

  with pytest.raises(side_questions.SideQuestionError) as failed:
    asyncio.run(side_questions.answer_side_question(plan, "/tmp"))

  assert failed.value.status_code == 502
  assert "session can't be copied" in failed.value.message


# ── Add to chat ──────────────────────────────────────────────────────────


def test_add_to_chat_stores_one_side_question_row_and_a_retry_reuses_it(client, auth, chat):
  body = {
    "cid": "side-1", "question": "what runs?", "answer": "The Kanban tests.",
    "provider": "claude",
  }

  first = client.post(f"/api/chats/{chat.id}/side-questions", headers=auth, json=body)
  again = client.post(f"/api/chats/{chat.id}/side-questions", headers=auth, json=body)

  assert first.status_code == 200, first.text
  assert again.status_code == 200, again.text
  rows = _rows(chat.id)
  assert len(rows) == 1
  row = rows[0]
  assert row["role"] == "user"
  assert row["kind"] == "side_question"
  assert row["side_question"] == {
    "question": "what runs?", "answer": "The Kanban tests.", "provider": "claude",
  }
  assert row["delivered_to_agent"] is False
  assert "The Kanban tests." in row["content"]
  assert again.json()["stored"]["cid"] == "side-1"


def test_add_to_chat_never_starts_a_turn_or_joins_the_queue(chat, db):
  _keep(chat.id)

  db.expire_all()
  stored = db.get(models.Chat, chat.id)
  assert not stored.pending_messages
  assert db.query(models.ChatRun).filter(models.ChatRun.chat_id == chat.id).count() == 0


def test_add_to_chat_waits_while_a_question_card_must_stay_last(client, auth, chat, db):
  chat.pending_question_id = "question-1"
  db.commit()

  response = client.post(f"/api/chats/{chat.id}/side-questions", headers=auth, json={
    "cid": "side-1", "question": "q", "answer": "a", "provider": "claude",
  })

  assert response.status_code == 409
  assert _rows(chat.id) == []


# ── Delivery to the agent ────────────────────────────────────────────────


def _admitted_run(chat_id, token):
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=token,
    user_msg={"role": "user", "content": "Carry on.", "ts": 10**13},
  )).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(chat_id=chat_id, run_token=token)).result(timeout=5)


def test_an_added_side_question_is_owed_until_a_successful_turn_carries_it(chat):
  _keep(chat.id, cid="side-1")
  assert [body["cid"] for _seq, body in _owed(chat.id)] == ["side-1"]

  _admitted_run(chat.id, "delivery-run")
  get_writer().submit(AcknowledgeProviderSuccess(
    chat_id=chat.id, run_token="delivery-run", side_question_cids=("side-1",),
  )).result(timeout=5)

  assert _owed(chat.id) == []
  row = next(body for body in _rows(chat.id) if body.get("cid") == "side-1")
  assert row["delivered_to_agent"] is True


def test_a_turn_without_provider_success_leaves_the_side_question_owed(chat):
  _keep(chat.id, cid="side-1")
  _admitted_run(chat.id, "failed-run")

  # No AcknowledgeProviderSuccess: the provider never confirmed the prompt.
  assert [body["cid"] for _seq, body in _owed(chat.id)] == ["side-1"]


class _Provider:
  name = "Claude Code"

  def check_auth(self, _data_dir):
    return None

  async def ensure_auth(self, _data_dir):
    return None

  def build_env(self, **_kwargs):
    return {}


@pytest.mark.asyncio
async def test_the_next_turn_carries_added_side_questions_once(monkeypatch, chat):
  from app import claude_sdk_runner

  _keep(chat.id, cid="side-1", question="what runs?", answer="The Kanban tests.")
  calls = []

  async def fake_runner(**kwargs):
    calls.append(kwargs)
    return {"session_id": "session-1", "cost_usd": 0.0, "error": None}

  async def fake_complete_turn(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  async def fake_record_run_metrics(**_kwargs):
    return None

  monkeypatch.setattr(chat_mod, "get_provider", lambda _id: _Provider())
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete_turn)
  monkeypatch.setattr(chat_mod, "_record_run_metrics", fake_record_run_metrics)
  monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)

  async def turn(token, text):
    get_writer().submit(StartTurn(
      chat_id=chat.id, run_token=token,
      user_msg={"role": "user", "content": text, "ts": 10**13 + len(calls), "cid": token},
      title_source=text, default_provider="claude",
    )).result(timeout=5)
    create_broadcast(chat.id)
    try:
      await chat_mod._run_chat_impl(
        messages=[schemas.ChatMessage(role="user", content=text)],
        chat_id=chat.id, session_id=None, provider_id="claude", run_token=token,
      )
    finally:
      remove_broadcast(chat.id)

  await turn("turn-1", "Carry on.")
  await turn("turn-2", "And then?")

  first, second = (call["user_message"] for call in calls)
  assert "<side_questions>" in first
  assert "Side question: what runs?" in first
  assert "The Kanban tests." in first
  assert "<side_questions>" not in second
  assert _owed(chat.id) == []
