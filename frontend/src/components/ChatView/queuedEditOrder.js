/**
 * Orders the writes to one queued message around its edits.
 *
 * Saving an edit is a round trip whose response hydrates the queue from server
 * truth. Anything else done to the same message while that save is in flight
 * would race it: a cancellation could be undone when the late response puts
 * the row back in the tray, and Send now could send the text the edit is
 * replacing. So each message's edits run one after another, and its other
 * actions wait for `settled(cid)` before they start. Messages never wait for
 * each other.
 */
export function createQueuedEditOrder() {
  // cid -> a promise that settles once every edit started so far has settled.
  const tails = new Map()

  return {
    /** Run one edit of a queued message after its earlier edits settle.
     * Resolves or rejects exactly as `edit()` does. */
    run(cid, edit) {
      const result = (tails.get(cid) || Promise.resolve()).then(edit)
      const tail = result.then(() => {}, () => {})
      tails.set(cid, tail)
      tail.then(() => {
        if (tails.get(cid) === tail) tails.delete(cid)
      })
      return result
    },

    /** Resolves once every edit of the message started so far has settled,
     * successfully or not; at once when none is in flight. Never rejects. */
    settled(cid) {
      return tails.get(cid) || Promise.resolve()
    },
  }
}
