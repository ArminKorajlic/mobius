// Queued messages are edited in the chat box; the tray only points there.
import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({ appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
after(() => vite.close())
const { default: QueuedMessages } = await vite.ssrLoadModule('/src/components/ChatView/QueuedMessages.jsx')

const items = [
  { cid: 'q1', content: 'Then check the dark theme' },
  { cid: 'q2', content: 'Also update the changelog' },
]

test('the queued message open in the chat box is tagged instead of offering the pencil', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, { items, inComposerCid: 'q2' }))
  assert.match(html, /queued__row queued__row--in-composer[^>]*>[\s\S]*Also update the changelog[\s\S]*In the chat box/)
  assert.equal(html.match(/aria-label="Edit queued message"/g)?.length, 1,
    'only the other row keeps its pencil')
})

test('the tray never renders its own editor', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, { items }))
  assert.doesNotMatch(html, /<textarea/)
  assert.equal(html.match(/aria-label="Edit queued message"/g)?.length, 2)
})
