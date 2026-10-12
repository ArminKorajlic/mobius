/* One chat's `/btw` side question: ask a temporary copy, then keep or drop the answer. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, jsonOrThrow } from '../../../api/client.js'

/** The owner-facing reason a side question or an "Add to chat" failed. */
export function sideQuestionFailure(error) {
  // The server's own words (a 4xx/5xx detail) say what to do next, e.g.
  // "This chat has nothing to answer from yet."
  if (typeof error?.status === 'number' && error.message) return error.message
  if (error?.name === 'TimeoutError') {
    return 'The side question took too long to answer. Try again.'
  }
  return 'Couldn’t reach Möbius. Check your connection and try again.'
}

/**
 * `sideQuestion` is null or `{ question, status, answer, provider, error }`,
 * where status is `asking`, `answered`, `adding` or `failed`. Only the latest
 * question owns the card: closing it or asking again makes an earlier answer
 * arrive into nothing, and changing chats drops the card. Nothing is saved
 * until `add()`; a retried add reuses its `cid`, so it cannot save twice.
 */
export default function useSideQuestion({ chatId, onAdded }) {
  const [sideQuestion, setSideQuestion] = useState(null)
  const requestRef = useRef(0)
  const currentRef = useRef(null)
  currentRef.current = sideQuestion
  const onAddedRef = useRef(onAdded)
  onAddedRef.current = onAdded

  useEffect(() => {
    requestRef.current += 1
    setSideQuestion(null)
  }, [chatId])

  const ask = useCallback(async (question) => {
    const request = ++requestRef.current
    const owns = () => requestRef.current === request
    setSideQuestion({ request, question, status: 'asking' })
    try {
      const answer = await jsonOrThrow(
        await api.chats.askSideQuestion(chatId, question),
        'Side question failed',
      )
      if (!owns()) return
      setSideQuestion({
        request,
        question: answer.question || question,
        answer: answer.answer,
        provider: answer.provider,
        cid: globalThis.crypto.randomUUID(),
        status: 'answered',
      })
    } catch (error) {
      if (!owns()) return
      setSideQuestion({
        request, question, status: 'failed', error: sideQuestionFailure(error),
      })
    }
  }, [chatId])

  const add = useCallback(async () => {
    const card = currentRef.current
    if (card?.status !== 'answered') return
    setSideQuestion({ ...card, status: 'adding', error: null })
    try {
      await jsonOrThrow(await api.chats.keepSideQuestion(chatId, {
        cid: card.cid,
        question: card.question,
        answer: card.answer,
        provider: card.provider,
      }), 'Adding the side question failed')
      if (requestRef.current !== card.request) return
      requestRef.current += 1
      setSideQuestion(null)
      onAddedRef.current?.()
    } catch (error) {
      if (requestRef.current !== card.request) return
      setSideQuestion({ ...card, status: 'answered', error: sideQuestionFailure(error) })
    }
  }, [chatId])

  const close = useCallback(() => {
    requestRef.current += 1
    setSideQuestion(null)
  }, [])

  const retry = useCallback(() => {
    const card = currentRef.current
    if (card?.question) void ask(card.question)
  }, [ask])

  return { sideQuestion, ask, add, close, retry }
}
