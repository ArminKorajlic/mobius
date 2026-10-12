/* A /btw answer reaches the chat only through Add to chat, and only once. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../../../api/client.js'
import useSideQuestion, { sideQuestionFailure } from '../useSideQuestion.js'
import { renderHook } from './react-hook-shim.mjs'

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setup(t) {
  const asks = [], keeps = [], added = []
  for (const name of ['askSideQuestion', 'keepSideQuestion']) {
    const original = api.chats[name]
    t.after(() => { api.chats[name] = original })
  }
  api.chats.askSideQuestion = (chatId, question) => {
    const pending = deferred()
    asks.push({ chatId, question, ...pending })
    return pending.promise
  }
  api.chats.keepSideQuestion = (chatId, payload) => {
    const pending = deferred()
    keeps.push({ chatId, payload, ...pending })
    return pending.promise
  }
  const hook = renderHook(useSideQuestion, { chatId: 'chat-a', onAdded: () => added.push('added') })
  t.after(() => hook.unmount())
  return { hook, asks, keeps, added }
}

const answer = { question: 'what runs?', answer: 'The Kanban tests.', provider: 'claude' }

test('an answer shows in the card and nothing is saved until Add to chat', async t => {
  const { hook, asks, keeps } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  assert.equal(hook.result.current.sideQuestion.status, 'asking')
  asks[0].resolve(Response.json(answer))
  await asking
  const card = hook.result.current.sideQuestion
  assert.equal(card.status, 'answered')
  assert.equal(card.answer, 'The Kanban tests.')
  assert.equal(keeps.length, 0)
})

test('Add to chat saves one row with a stable cid and then closes the card', async t => {
  const { hook, asks, keeps, added } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(answer))
  await asking
  const adding = hook.result.current.add()
  assert.equal(hook.result.current.sideQuestion.status, 'adding')
  await hook.result.current.add()
  assert.equal(keeps.length, 1, 'a second press while adding sends nothing')
  const { cid, ...rest } = keeps[0].payload
  assert.ok(cid)
  assert.deepEqual(rest, { question: 'what runs?', answer: 'The Kanban tests.', provider: 'claude' })
  keeps[0].resolve(Response.json({ stored: { cid } }))
  await adding
  assert.equal(hook.result.current.sideQuestion, null)
  assert.deepEqual(added, ['added'])
})

test('a failed add keeps the answer and retries with the same cid', async t => {
  const { hook, asks, keeps } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(answer))
  await asking
  const first = hook.result.current.add()
  keeps[0].resolve(Response.json(
    { detail: 'Answer the open question first, then add the side question.' },
    { status: 409 },
  ))
  await first
  assert.equal(hook.result.current.sideQuestion.status, 'answered')
  assert.match(hook.result.current.sideQuestion.error, /Answer the open question first/)
  const second = hook.result.current.add()
  assert.equal(keeps[1].payload.cid, keeps[0].payload.cid)
  keeps[1].resolve(Response.json({ stored: {} }))
  await second
  assert.equal(hook.result.current.sideQuestion, null)
})

test('closing while the copy is answering drops its late answer', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  hook.result.current.close()
  asks[0].resolve(Response.json(answer))
  await asking
  await flush()
  assert.equal(hook.result.current.sideQuestion, null)
})

test('the server says why a question could not be answered, and Try again asks again', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(
    { detail: 'This chat has nothing to answer from yet. Ask again after its first reply.' },
    { status: 409 },
  ))
  await asking
  assert.equal(hook.result.current.sideQuestion.status, 'failed')
  assert.match(hook.result.current.sideQuestion.error, /nothing to answer from yet/)
  hook.result.current.retry()
  assert.equal(asks.length, 2)
  assert.equal(asks[1].question, 'what runs?')
})

test('switching chats drops the card', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  hook.rerender({ chatId: 'chat-b', onAdded: () => {} })
  asks[0].resolve(Response.json(answer))
  await asking
  await flush()
  assert.equal(hook.result.current.sideQuestion, null)
})

test('transport failures read as connection or time problems', () => {
  const timeout = new Error('Request timed out')
  timeout.name = 'TimeoutError'
  assert.match(sideQuestionFailure(timeout), /took too long/)
  assert.match(sideQuestionFailure(new TypeError('Failed to fetch')), /Check your connection/)
})
