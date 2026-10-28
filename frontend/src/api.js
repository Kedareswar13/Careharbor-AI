// API client. In dev, Vite proxies /api to FastAPI (port 8010), so the staff session cookie is same-origin.
// Writes send the CSRF header the server requires for cookie-authenticated requests.
const CSRF = { 'X-CareHarbor-CSRF': '1' }

async function handle(r) {
  if (!r.ok) throw Object.assign(new Error((await r.json().catch(() => ({}))).detail || r.statusText), { status: r.status })
  return r.json()
}

export const getJSON = (path) => fetch(path, { credentials: 'same-origin' }).then(handle)

export const postJSON = (path, body) =>
  fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...CSRF }, body: JSON.stringify(body) }).then(handle)

// Server-Sent Events over fetch: calls onEvent for each {type: start|stage|result|error} event.
export async function streamChat(body, onEvent) {
  const r = await fetch('/api/chat/stream', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!r.ok || !r.body) throw Object.assign(new Error((await r.json().catch(() => ({}))).detail || r.statusText), { status: r.status })
  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i)
      buf = buf.slice(i + 2)
      const line = chunk.split('\n').find((l) => l.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)))
    }
  }
}
