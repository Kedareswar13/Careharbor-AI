import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, ArrowDown, ArrowLeft, BookOpen, CalendarClock, CalendarX, Check, ChevronDown, ChevronRight, CircleDashed, ClipboardCheck, Copy, Cpu, Database, Download,
  Eraser, FileSearch, Flag, HelpCircle, History, Lock, LogOut, MessageSquare, Mic, PanelRightClose, PanelRightOpen, Pill, RotateCcw, Send, ShieldCheck, SquarePen,
  Stethoscope, ThumbsDown, ThumbsUp, Trash2, UserRound, Users, Volume2,
} from 'lucide-react'
import { getJSON, postJSON, streamChat } from './api.js'
import Card from './Cards.jsx'
import Markdown from './Markdown.jsx'

const NEXT_STAGE = { safety: 'Understanding the request', understand: 'Looking up records and documents', retrieve: 'Writing a grounded answer', records: 'Preparing the answer', identity: 'Preparing the answer' }
const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`)
const timeNow = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const fmtWhen = (iso) => {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ', ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// The browser keeps only the list of signed conversation ids; transcripts live on the clinic server (survive refresh / restart).
const SESSION_KEY = 'careharbor_session'
const CONV_KEY = 'careharbor_conversations'
const readConvs = () => { try { return JSON.parse(localStorage.getItem(CONV_KEY) || '[]') } catch { return [] } }

function fromHistory(h) {
  const lastA = h.messages.map((m) => m.role).lastIndexOf('assistant')
  return h.messages.map((m, i) => {
    const time = fmtWhen(m.ts)
    if (m.role === 'user') return { id: `h${m.id}`, role: 'user', text: m.text, time }
    const live = i === lastA && (m.result?.card?.type !== 'confirm' || Boolean(h.pending))
    return { id: m.result?.turn_id || `h${m.id}`, role: 'assistant', text: m.text, time, answered: !live, result: { ...m.result, session_id: h.session_id } }
  })
}

const ACTIONS = [
  { icon: <Stethoscope size={16} />, title: 'Check a symptom', sub: 'Safety screen first, then guidance from clinic leaflets', text: "I've had a headache since morning, what should I do?" },
  { icon: <CalendarClock size={16} />, title: 'Book an appointment', sub: 'Verify identity, pick a slot, confirm before saving', text: 'I visited last month, can I see Dr. Rao again on Friday?' },
  { icon: <CalendarX size={16} />, title: 'Reschedule or cancel', sub: 'Change an existing visit (2-hour cutoff applies)', text: 'I need to reschedule my appointment' },
  { icon: <Pill size={16} />, title: 'My prescriptions', sub: 'Read back exactly as your doctor recorded', text: 'When do I take the antibiotic the doctor gave me?' },
  { icon: <HelpCircle size={16} />, title: 'Clinic information', sub: 'Timings, fees, insurance, visiting rules', text: 'What are your OPD timings? Do you accept Star Health insurance?' },
  { icon: <Users size={16} />, title: 'Talk to a staff member', sub: 'Creates a ticket in the front-desk queue', text: 'I want to talk to a person', danger: true },
]

export function grounding(res) {
  if (!res) return null
  const card = res.card || {}
  if (res.kind === 'emergency') return { cls: 'emergency', label: 'Emergency rule' }
  if (['blocked', 'out_of_scope'].includes(res.intent) || card.type === 'out_of_scope') return { cls: 'safety', label: 'Safety guard' }
  if (res.guardrails?.some((g) => g.type === 'output_guard')) return { cls: 'safety', label: 'Quoted from source' }
  if (res.citations?.length) return { cls: 'doc', label: `Grounded · ${res.citations.length} source${res.citations.length > 1 ? 's' : ''}` }
  if (['prescriptions', 'result', 'confirm', 'slots', 'appointments'].includes(card.type)) return { cls: 'record', label: 'Clinic records' }
  if (['otp', 'identity'].includes(card.type)) return { cls: 'record', label: 'Identity check' }
  return { cls: 'neutral', label: 'CareHarbor AI' }
}

/* ------------------------------------------------------------------ evidence & safety panel */
function safetyChecks(res, verified) {
  const g = res.guardrails || []
  const retr = (res.stages || []).find((s) => s.key === 'retrieve')
  const checks = []
  checks.push(res.kind === 'emergency'
    ? { st: 'fail', title: 'Red-flag screen', note: `Emergency signs detected (${(res.card?.categories || []).join(', ')}). Staff alerted.` }
    : { st: 'pass', title: 'Red-flag screen', note: 'Deterministic rules ran before any AI model. No warning signs.' })
  const q = g.find((x) => x.type === 'query_guard')
  checks.push(q ? { st: 'warn', title: 'Query validation', note: `Blocked: ${q.reason.replace(/_/g, ' ')}` } : { st: 'pass', title: 'Query validation', note: 'No injection, exfiltration or off-topic pattern.' })
  checks.push({ st: 'pass', title: 'Access control', note: `Documents filtered for role "${verified ? 'patient (verified)' : 'anonymous'}" and this clinic only.` })
  const og = g.find((x) => x.type === 'output_guard')
  if (og) checks.push({ st: 'warn', title: 'Output guard', note: `Model answer replaced by a source quote (${og.violations.join(', ') || 'empty answer'}).` })
  else if (res.llm_usage?.length) checks.push({ st: 'pass', title: 'Output guard', note: 'No diagnosis, dosing or new-medicine advice; numbers and claims grounded.' })
  else checks.push({ st: 'na', title: 'Output guard', note: 'Template reply built from clinic records - no generated text.' })
  if (retr) checks.push({ st: retr.grade === 'low' ? 'warn' : 'pass', title: 'Retrieval confidence', note: `Grade ${retr.grade || 'n/a'} · ${retr.pipeline || 'basic'} pipeline${retr.corrective ? ' · corrective retry used' : ''}` })
  if (res.card?.type === 'confirm') checks.push({ st: 'warn', title: 'Human confirmation', note: 'Waiting for your confirmation - nothing has been saved yet.' })
  if (res.card?.type === 'result') checks.push({ st: 'pass', title: 'Database confirmation', note: 'Reply built from the stored row after re-reading it.' })
  return checks
}

const SAFEGUARDS = [
  ['Red-flag screen', 'Deterministic emergency rules run before any AI model on every message.'],
  ['Query validation', 'Prompt-injection, data-exfiltration and off-topic requests are refused.'],
  ['Access control', 'Documents filtered by role and clinic before retrieval; records only after OTP.'],
  ['Grounded answers', 'Clinic questions are answered only from retrieved documents, with sources.'],
  ['Output guard', 'No diagnosis, dosing or new-medicine advice; unsupported numbers are replaced.'],
  ['Human confirmation', 'Bookings, changes and cancellations wait for your Confirm.'],
  ['Privacy', 'Phone numbers, dates of birth and codes are masked in logs and saved history.'],
]

function SessionOverview({ health, messages, session, verified, title }) {
  const answers = messages.filter((m) => m.role === 'assistant' && m.result)
  const grounded = answers.filter((m) => m.result.citations?.length).length
  const timed = answers.filter((m) => m.result.total_ms)
  const avg = timed.length ? timed.reduce((a, m) => a + m.result.total_ms, 0) / timed.length : 0
  const tools = answers.reduce((a, m) => a + (m.result.tool_calls?.length || 0), 0)
  const guards = answers.reduce((a, m) => a + (m.result.guardrails?.length || 0), 0)
  const emergencies = answers.filter((m) => m.result.kind === 'emergency').length
  const col = health?.collections || {}
  return (
    <>
      <div className="pane-title">This conversation</div>
      <div className="kvs">
        <span>Topic</span><span className="plain">{title || (messages.length ? 'Conversation' : 'Not started')}</span>
        <span>Identity</span><span className="plain">{verified ? `Verified · ${verified}` : 'Not verified'}</span>
        <span>Messages</span><span>{messages.filter((m) => m.role === 'user').length}</span>
        <span>Session</span><span>{session ? session.slice(0, 10) : '-'}</span>
        <span>Saved</span><span className="plain">{session ? 'On the clinic server. Survives refresh and signing in again.' : 'Starts with your first message'}</span>
      </div>
      {answers.length > 0 && (
        <>
          <div className="pane-title">Answer quality so far</div>
          <div className="ov-grid">
            <div className="ov"><b>{answers.length}</b><small>answers</small></div>
            <div className="ov"><b>{grounded}</b><small>with sources</small></div>
            <div className="ov"><b>{fmtMs(avg)}</b><small>avg. response</small></div>
            <div className="ov"><b>{tools}</b><small>record look-ups</small></div>
            <div className="ov"><b>{guards}</b><small>guard actions</small></div>
            <div className={`ov ${emergencies ? 'alert' : ''}`}><b>{emergencies}</b><small>emergencies</small></div>
          </div>
        </>
      )}
      <div className="pane-title">Active safeguards</div>
      {SAFEGUARDS.map(([t, n]) => (
        <div className="check" key={t}><span className="st pass"><Check size={15} /></span><span><b>{t}</b><small>{n}</small></span></div>
      ))}
      <div className="pane-title">Knowledge base</div>
      {health ? (
        <div className="kvs">
          <span>Documents</span><span>{health.documents} ({health.sections || '-'} sections)</span>
          <span>Care leaflets</span><span>{col.leaflets ?? '-'}</span>
          <span>MedlinePlus</span><span>{col.medlineplus ?? '-'}</span>
          <span>Clinic policies</span><span>{col.policies ?? '-'}</span>
          {col.staff != null && (<><span>Staff SOPs</span><span>{col.staff} (staff only)</span></>)}
          <span>Retrieval</span><span>{health.rag_mode} · {health.rag_pipeline || 'basic'}</span>
          <span>Answer model</span><span>{health.model}</span>
        </div>
      ) : <p className="ev-empty">The clinic server is not reachable.</p>}
      <p className="ev-hint"><FileSearch size={14} /> Select <b>Evidence</b> under any answer to see its safety checks, sources, evidence map and stage timings.</p>
    </>
  )
}

function EvidencePanel({ res, verified, onBack, ...overview }) {
  if (!res) {
    return (
      <aside className="pane-right">
        <SessionOverview verified={verified} {...overview} />
      </aside>
    )
  }
  const retr = (res.stages || []).find((s) => s.key === 'retrieve')
  const tokens = (res.llm_usage || []).reduce((a, u) => a + (u.in || 0) + (u.out || 0), 0)
  const icon = { pass: <Check size={15} />, warn: <AlertTriangle size={15} />, fail: <AlertTriangle size={15} />, na: <CircleDashed size={15} /> }
  return (
    <aside className="pane-right">
      <button className="linkbtn" onClick={onBack} title="Show the conversation overview"><ArrowLeft size={14} /> Conversation overview</button>
      <div className="pane-title">Safety checks for this answer</div>
      {safetyChecks(res, verified).map((c) => (
        <div className="check" key={c.title}><span className={`st ${c.st}`}>{icon[c.st]}</span><span><b>{c.title}</b><small>{c.note}</small></span></div>
      ))}
      <div className="pane-title">Sources</div>
      {res.citations?.length ? res.citations.map((c, i) => (
        <div className="src" key={c.id}><b>{i + 1}. {c.title}</b><small>{c.section} · {c.source}</small><br />
          <small>{c.url?.startsWith('http') ? <a href={c.url} target="_blank" rel="noreferrer">Open source document</a> : c.url}</small></div>
      )) : <p className="ev-empty">No documents used for this reply.</p>}
      {res.evidence?.length > 0 && (
        <>
          <div className="pane-title">Evidence map</div>
          {res.evidence.map((e, i) => (
            <div className="evmap" key={i}>
              <div className="q">"{e.sentence}"</div>
              <div className="a">{e.source} · support {Math.round(e.support * 100)}%</div>
              <div className="bar"><i style={{ width: `${Math.round(e.support * 100)}%` }} /></div>
            </div>
          ))}
        </>
      )}
      <div className="pane-title">Pipeline timeline</div>
      <div className="timeline">{(res.stages || []).map((s, i) => <div className="tl" key={i}><span>{s.label}</span><span>{fmtMs(s.ms)}</span></div>)}</div>
      <div className="pane-title">Details</div>
      <div className="kvs">
        <span>Intent</span><span>{res.intent || '-'}</span>
        <span>Routed by</span><span>{res.route_source || '-'}</span>
        <span>Model</span><span>{res.llm_usage?.length ? res.model : 'none (deterministic)'}</span>
        <span>Tokens</span><span>{tokens || 0}</span>
        <span>Tools</span><span>{(res.tool_calls || []).map((t) => t.tool).join(', ') || '-'}</span>
        {retr?.rewritten && (<><span>Searched as</span><span>{retr.rewritten}</span></>)}
        <span>Total time</span><span>{fmtMs(res.total_ms || 0)}</span>
      </div>
    </aside>
  )
}

/* ------------------------------------------------------------------ conversation pieces */
function Processing({ stages }) {
  const last = stages[stages.length - 1]
  return (
    <div className="row">
      <div className="avatar ch">CH</div>
      <div className="processing" aria-live="polite">
        <div className="processing-head"><span className="spinner" /> Processing your request</div>
        {stages.map((s, i) => <div className="step" key={i}><Check size={14} color="var(--ok)" /> {s.label}<span className="ms">{fmtMs(s.ms)}</span></div>)}
        <div className="step active"><span className="spinner" /> {last ? NEXT_STAGE[last.key] || 'Finishing up' : 'Running the safety screen'}…</div>
      </div>
    </div>
  )
}

function AssistantRow({ m, selected, onSelect, onSend, onConfirm, demo }) {
  const res = m.result || {}
  const g = grounding(res)
  const [rated, setRated] = useState(null)
  const [copied, setCopied] = useState(false)
  const rate = async (rating) => { setRated(rating); try { await postJSON('/api/feedback', { session_id: res.session_id, turn_id: res.turn_id || 'na', rating }) } catch { /* best effort */ } }
  const speak = () => { if ('speechSynthesis' in window) { window.speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(m.text.replace(/[*#|]/g, '')); u.lang = 'en-IN'; window.speechSynthesis.speak(u) } }
  const copy = async () => { await navigator.clipboard?.writeText(m.text); setCopied(true); setTimeout(() => setCopied(false), 1200) }
  return (
    <div className="row">
      <div className="avatar ch">CH</div>
      <div>
        <div className="who"><b>CareHarbor AI</b><span>{m.time}</span></div>
        <div className={`answer g-${g.cls} ${selected ? 'selected' : ''}`}>
          <div className="tags">
            <span className={`tag ${g.cls}`}>{g.label}</span>
            {res.intent && <span className="tag neutral">{res.intent.replace('_', ' ')}</span>}
            {res.stages?.some((s) => s.key === 'cache') && <span className="tag ok">from answer cache</span>}
          </div>
          <Markdown text={m.text} />
          <Card card={res.card} answered={m.answered} onSend={onSend} onConfirm={onConfirm} demo={demo} />
          <div className="actions-row">
            <button className="abtn" onClick={copy} title="Copy the answer">{copied ? <Check size={14} /> : <Copy size={14} />} {copied ? 'Copied' : 'Copy'}</button>
            <button className="abtn" onClick={speak} title="Read the answer aloud"><Volume2 size={14} /> Read aloud</button>
            <button className={`abtn ${selected ? 'on' : ''}`} onClick={onSelect} title="Show safety checks, sources and timings for this answer"><FileSearch size={14} /> Evidence{res.citations?.length ? ` (${res.citations.length})` : ''}</button>
            <button className={`abtn ${rated === 'up' ? 'on' : ''}`} onClick={() => rate('up')} title="This answer was helpful"><ThumbsUp size={14} /> Helpful</button>
            <button className={`abtn ${rated === 'down' ? 'on' : ''}`} onClick={() => rate('down')} title="This answer was not helpful"><ThumbsDown size={14} /> Not helpful</button>
            <button className={`abtn ${rated === 'report' ? 'on' : ''}`} onClick={() => rate('report')} title="Report a safety or accuracy problem to staff"><Flag size={14} /> Report</button>
            {res.total_ms != null && <span className="timing">{fmtMs(res.total_ms)}</span>}
          </div>
        </div>
      </div>
    </div>
  )
}

function Composer({ onSend, busy, models, model, setModel }) {
  const [text, setText] = useState('')
  const [menu, setMenu] = useState(false)
  const [rec, setRec] = useState(null)
  const ta = useRef(null)
  useEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
    el.style.overflowY = el.scrollHeight > 160 ? 'auto' : 'hidden'
  }, [text])
  const submit = () => { const t = text.trim(); if (!t || busy) return; onSend(t); setText('') }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition
  const toggleMic = () => {
    if (!SR) { alert('Dictation needs a browser with the Web Speech API (Chrome or Edge).'); return }
    if (rec) { rec.stop(); setRec(null); return }
    const r = new SR()
    r.lang = 'en-IN'
    r.interimResults = true
    r.onresult = (e) => setText(Array.from(e.results).map((x) => x[0].transcript).join(' '))
    r.onend = () => setRec(null)
    r.start()
    setRec(r)
  }
  const describe = (m) => (m.default ? 'Default · best wording' : m.name.includes('1.5b') ? 'Faster · shorter answers' : 'Alternative local model')
  return (
    <div className="composer">
      <div className="composer-inner">
        <textarea ref={ta} rows={1} value={text} aria-label="Message" placeholder="Describe a symptom, ask about an appointment or your medicines, or ask a clinic question…"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }} />
        <div className="toolbar">
          <button className={`tbtn ${rec ? 'rec' : ''}`} onClick={toggleMic} title="Dictate your message (speech to text)"><Mic size={14} /> {rec ? 'Stop dictation' : 'Dictate'}</button>
          <button className="tbtn" onClick={() => setMenu((v) => !v)} title="Choose the AI model that writes answers"><Cpu size={14} /> Model: {model ? model.replace(':latest', '') : '…'} <ChevronDown size={13} /></button>
          {menu && (
            <div className="menu" onMouseLeave={() => setMenu(false)}>
              <div className="menu-title">Local models (Ollama)</div>
              {models.map((m) => (
                <button key={m.name} onClick={() => { setModel(m.name); setMenu(false) }}>
                  {m.name === model ? <Check size={15} color="var(--primary)" /> : <span style={{ width: 15 }} />}
                  <span><b>{m.name}</b><small>{describe(m)}{m.params ? ` · ${m.params}` : ''}{m.size_gb ? ` · ${m.size_gb} GB` : ''}</small></span>
                </button>
              ))}
              <div className="menu-title" style={{ textTransform: 'none', letterSpacing: 0 }}>Safety rules, identity, bookings and prescriptions never depend on the model.</div>
            </div>
          )}
          <span className="hint">Enter to send · Shift+Enter for a new line</span>
          <button className="send" disabled={!text.trim() || busy} onClick={submit}><Send size={14} /> Send</button>
        </div>
      </div>
      <div className="disclaimer">CareHarbor AI does not diagnose or prescribe. Demo with synthetic data. In an emergency call 112 / 108.</div>
    </div>
  )
}

/* ------------------------------------------------------------------ workspace */
export default function Workspace({ config, health, verified, onVerified }) {
  const [session, setSession] = useState(() => localStorage.getItem(SESSION_KEY) || '')
  const [messages, setMessages] = useState([])
  const [convs, setConvs] = useState(readConvs)
  const [linked, setLinked] = useState([])
  const [loading, setLoading] = useState(false)
  const [panel, setPanel] = useState(() => localStorage.getItem('careharbor_panel') !== '0')
  const [selected, setSelected] = useState(null)
  const [stages, setStages] = useState(null)
  const [busy, setBusy] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const [models, setModels] = useState([])
  const [model, setModel] = useState(() => localStorage.getItem('careharbor_model') || '')
  const thread = useRef(null)
  // Bumped whenever the visible conversation changes (new, switch, sign out): late stream events from the old one are ignored.
  const epoch = useRef(0)
  const [notice, setNotice] = useState('')

  useEffect(() => { if (session) localStorage.setItem(SESSION_KEY, session); else localStorage.removeItem(SESSION_KEY) }, [session])
  useEffect(() => { localStorage.setItem(CONV_KEY, JSON.stringify(convs)) }, [convs])
  useEffect(() => { localStorage.setItem('careharbor_panel', panel ? '1' : '0') }, [panel])
  useEffect(() => { if (model) localStorage.setItem('careharbor_model', model) }, [model])
  useEffect(() => {
    let tries = 0
    const load = () => getJSON('/api/models').then((d) => {
      setModels(d.models)
      setModel((cur) => (cur && d.models.some((m) => m.name === cur) ? cur : (d.models.find((m) => m.default) || d.models[0])?.name || ''))
    }).catch(() => { if (tries++ < 20) setTimeout(load, 3000) })
    load()
  }, [])

  // Titles, counts and "signed out elsewhere" come from the server; a failed request keeps the local list.
  const refreshConvs = useCallback(async (extra) => {
    const ids = [...new Set([...(extra ? [extra] : []), ...readConvs().map((c) => c.session_id)])]
    if (!ids.length) return
    try {
      const { conversations } = await postJSON('/api/chat/conversations', { session_ids: ids })
      setConvs(conversations.map(({ session_id, title, updated_at, n }) => ({ session_id, title, updated_at, n })))
    } catch { /* offline: keep the local list */ }
  }, [])

  const showConversation = (h) => {
    epoch.current += 1
    const list = fromHistory(h)
    setMessages(list)
    setSession(h.session_id)
    setNotice('')
    onVerified(h.verified_name || null)
    const lastA = [...list].reverse().find((m) => m.role === 'assistant')
    setSelected(lastA ? lastA.id : null)
    setAtBottom(true)
  }

  const openConversation = useCallback(async (sid) => {
    if (!sid) return
    setLoading(true)
    try {
      const h = await getJSON(`/api/chat/history?session_id=${encodeURIComponent(sid)}`)
      if (h.found && !h.ended && !h.locked) showConversation(h)
      else {
        epoch.current += 1
        setConvs((cs) => cs.filter((c) => c.session_id !== sid)); setMessages([]); setSession(''); setSelected(null); onVerified(null)
        if (h.locked) setNotice('Your last conversation contains personal records and its verification has expired, so it is hidden on this device. Verify again (phone, date of birth and code) and it will appear under "Your earlier conversations".')
      }
    } catch (e) {
      if (e.status === 404) { setConvs((cs) => cs.filter((c) => c.session_id !== sid)); setSession('') }
    } finally { setLoading(false) }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // On page load / refresh: restore the open conversation and the conversation list from the server.
  useEffect(() => { if (session) openConversation(session); refreshConvs() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Once verified, offer the patient's earlier conversations (also those started on other devices).
  useEffect(() => {
    if (!verified || !session) { setLinked([]); return }
    getJSON(`/api/chat/linked?session_id=${encodeURIComponent(session)}`).then((d) => setLinked(d.conversations)).catch(() => setLinked([]))
  }, [verified, session, convs.length])

  const scrollDown = useCallback((force) => { const el = thread.current; if (el && (force || atBottom)) el.scrollTop = el.scrollHeight }, [atBottom])
  useEffect(() => { scrollDown() }, [messages, stages]) // eslint-disable-line react-hooks/exhaustive-deps
  const onScroll = () => { const el = thread.current; setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80) }

  const markAnswered = (list) => list.map((m) => (m.role === 'assistant' ? { ...m, answered: true } : m))
  const addAssistant = (result, ep = epoch.current) => {
    if (ep !== epoch.current) return  // the user moved on (signed out, switched or started a new conversation)
    if (result.session_id) setSession(result.session_id)
    if ('verified_name' in result) onVerified(result.verified_name || null)
    const id = result.turn_id || `a${Date.now()}`
    setMessages((prev) => [...prev, { id, role: 'assistant', text: result.reply || '', result, time: timeNow() }])
    setSelected(id)
    if (result.session_id) refreshConvs(result.session_id)
  }
  const send = async (text) => {
    if (busy) return
    const ep = epoch.current
    const sent = session
    setNotice('')
    setMessages((prev) => [...markAnswered(prev), { id: `u${Date.now()}`, role: 'user', text, time: timeNow() }])
    setBusy(true); setStages([]); setAtBottom(true)
    try {
      await streamChat({ message: text, session_id: session || undefined, model: model || undefined }, (ev) => {
        if (ep !== epoch.current) return
        if (ev.type === 'start' && ev.session_id) {
          if (sent && ev.session_id !== sent) {
            // The server closed the old conversation (signed out elsewhere, or a new server secret): this message starts a new one.
            setMessages((prev) => prev.slice(-1)); setSelected(null); onVerified(null)
            setNotice('Your previous conversation was closed on the server, so this message started a new conversation.')
          }
          setSession(ev.session_id)
        }
        if (ev.type === 'stage') setStages((s) => [...(s || []), ev])
        if (ev.type === 'result') addAssistant(ev.data, ep)
        if (ev.type === 'error') addAssistant({ reply: ev.message, kind: 'template' }, ep)
      })
    } catch (e) {
      addAssistant({ reply: e.status === 429 ? 'Too many messages - please wait a moment.' : 'Sorry, the clinic server could not be reached. Please try again.', kind: 'template' }, ep)
    } finally { setBusy(false); setStages(null) }
  }
  const confirm = async (decision) => {
    if (busy || !session) return
    const ep = epoch.current
    setMessages((prev) => [...markAnswered(prev), { id: `u${Date.now()}`, role: 'user', text: { confirm: 'Confirm', change: 'Show other times', cancel: 'Cancel' }[decision], time: timeNow() }])
    setBusy(true); setStages([])
    try { addAssistant({ ...(await postJSON('/api/chat/confirm', { session_id: session, decision, model: model || undefined })), session_id: session }, ep) }
    catch (e) { addAssistant({ reply: e.message || 'That confirmation has expired. Please choose again.', kind: 'template' }, ep) }
    finally { setBusy(false); setStages(null) }
  }
  const newConversation = () => { epoch.current += 1; setMessages([]); setSession(''); setSelected(null); setNotice(''); onVerified(null) }
  const switchTo = (sid) => { if (!busy && sid !== session) openConversation(sid) }
  const deleteConversation = async (sid) => {
    if (!window.confirm('Delete this conversation from the clinic server? This cannot be undone.')) return
    try { await postJSON('/api/chat/delete', { session_id: sid }) } catch { /* already gone */ }
    setConvs((cs) => cs.filter((c) => c.session_id !== sid))
    if (sid === session) newConversation()
  }
  const reopen = async (sid) => {
    setLoading(true)
    try {
      const h = await postJSON('/api/chat/reopen', { session_id: sid, via: session })
      showConversation(h)
      refreshConvs(sid)
    } catch (e) { alert(e.message || 'Could not open that conversation.') } finally { setLoading(false) }
  }
  const signOut = async () => {
    const ids = [...new Set([session, ...convs.map((c) => c.session_id)].filter(Boolean))]
    try { await postJSON('/api/chat/signout', { session_ids: ids }) } catch { /* local sign-out still applies */ }
    setConvs([]); setLinked([]); newConversation()
  }
  const download = () => {
    const body = messages.map((m) => `[${m.time}] ${m.role === 'user' ? 'You' : 'CareHarbor AI'}:\n${m.text}\n`).join('\n')
    const url = URL.createObjectURL(new Blob([`CareHarbor AI conversation (${new Date().toLocaleString()})\nDemo with synthetic data - not medical advice.\n\n${body}`], { type: 'text/plain' }))
    Object.assign(document.createElement('a'), { href: url, download: 'careharbor-conversation.txt' }).click()
    URL.revokeObjectURL(url)
  }
  const selectedRes = useMemo(() => messages.find((m) => m.id === selected)?.result, [messages, selected])
  const current = convs.find((c) => c.session_id === session)
  const known = new Set(convs.map((c) => c.session_id))
  const earlier = linked.filter((c) => !known.has(c.session_id))

  return (
    <div className={`workspace ${panel ? '' : 'no-right'}`}>
      <aside className="pane-left">
        <div className="pane-title">Visit status</div>
        <div className="status-box">
          <div className="status-row">
            {verified ? <ShieldCheck size={18} className="badge-ok" /> : <Lock size={18} className="badge-off" />}
            <span>{verified ? `Verified · ${verified}` : 'Not verified'}<small>{verified ? 'Your records can be shown in this conversation.' : 'Records need phone, date of birth and a one-time code.'}</small></span>
          </div>
          {verified && <button className="linkbtn danger" onClick={signOut} disabled={busy} title="End verification on this device. Your conversations come back when you verify again."><LogOut size={14} /> Sign out on this device</button>}
        </div>
        <div className="pane-title">What would you like to do?</div>
        {ACTIONS.map((a) => (
          <button key={a.title} className={`action ${a.danger ? 'danger' : ''}`} disabled={busy} onClick={() => send(a.text)} title={a.text}>
            <span className="ic">{a.icon}</span><span><b>{a.title}</b><small>{a.sub}</small></span><ChevronRight size={15} className="chev" />
          </button>
        ))}
        {config?.demo_mode && (
          <>
            <div className="pane-title">Demo identities (synthetic)</div>
            <div className="demo-box">
              <button onClick={() => navigator.clipboard?.writeText('9876500001, 14/05/1990')} title="Copy to clipboard"><span><UserRound size={13} /> Aarav (antibiotic, Dr. Rao)</span><code>9876500001 · 14/05/1990</code></button>
              <button onClick={() => navigator.clipboard?.writeText('9876500002, 02/11/1985')} title="Copy to clipboard"><span><UserRound size={13} /> Ananya (upcoming visit)</span><code>9876500002 · 02/11/1985</code></button>
              <button onClick={() => navigator.clipboard?.writeText('9000000001, 01/01/2000')} title="Copy to clipboard"><span><UserRound size={13} /> QA test account</span><code>9000000001 · 01/01/2000</code></button>
            </div>
          </>
        )}
        <div className="pane-title">Conversations</div>
        <button className="action" onClick={newConversation} disabled={busy}><span className="ic"><SquarePen size={16} /></span><span><b>New conversation</b><small>Starts a fresh, unverified session. This one stays in the list.</small></span></button>
        {convs.length > 0 && (
          <div className="conv-list" aria-label="Recent conversations">
            {convs.map((c) => (
              <div key={c.session_id} className={`conv-item ${c.session_id === session ? 'active' : ''}`}>
                <button className="conv-open" onClick={() => switchTo(c.session_id)} disabled={busy} title="Open this conversation">
                  <MessageSquare size={14} /><span><b>{c.title}</b><small>{fmtWhen(c.updated_at)} · {c.n} message{c.n === 1 ? '' : 's'}</small></span>
                </button>
                <button className="conv-del" onClick={() => deleteConversation(c.session_id)} disabled={busy} title="Delete this conversation" aria-label="Delete conversation"><Trash2 size={14} /></button>
              </div>
            ))}
          </div>
        )}
        {earlier.length > 0 && (
          <>
            <div className="pane-title"><History size={12} /> Your earlier conversations</div>
            <div className="conv-list">
              {earlier.map((c) => (
                <div key={c.session_id} className="conv-item">
                  <button className="conv-open" onClick={() => reopen(c.session_id)} disabled={busy || loading} title="Re-open (you are verified in this conversation)">
                    <RotateCcw size={14} /><span><b>{c.title}</b><small>{fmtWhen(c.updated_at)} · {c.n} message{c.n === 1 ? '' : 's'}</small></span>
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
        <div className="pane-title">Tools</div>
        <button className="action" onClick={download} disabled={!messages.length}><span className="ic"><Download size={16} /></span><span><b>Download transcript</b><small>Saves this conversation as a text file</small></span></button>
        <button className="action" onClick={() => send('Please forget my preferences')} disabled={busy}><span className="ic"><Eraser size={16} /></span><span><b>Erase my preferences</b><small>Deletes remembered choices (right to erasure)</small></span></button>
      </aside>

      <section className="convo">
        <div className="convo-head">
          <ClipboardCheck size={18} color="var(--primary)" />
          <h2>{current?.title && messages.length ? current.title : 'Front desk conversation'}</h2>
          <span className="meta">{session ? `session ${session.slice(0, 8)}` : 'new session'} · {messages.filter((m) => m.role === 'user').length} messages</span>
          <span className="spacer" />
          <span className="tag neutral" title="Conversations are saved on the clinic server"><Database size={12} /> Saved</span>
          <button className="abtn" onClick={() => setPanel((v) => !v)} title={panel ? 'Hide the evidence & safety panel' : 'Show the evidence & safety panel'}>
            {panel ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />} {panel ? 'Hide panel' : 'Evidence panel'}
          </button>
        </div>
        <div className="thread" ref={thread} onScroll={onScroll}>
          <div className="thread-inner">
            {loading && <div className="loading-note"><span className="spinner" /> Loading your conversation…</div>}
            {notice && <div className="notice-card" role="status"><Lock size={15} /> <span>{notice}</span></div>}
            {!loading && messages.length === 0 && (
              <div className="welcome">
                <h1>How can the front desk help you today?</h1>
                <p>CareHarbor AI is the AI front desk of {config?.clinic || 'the clinic'}. Choose a task on the left or type below. Every answer shows its sources and safety checks on the right.</p>
                <div className="safeguards">
                  <div className="safeguard"><AlertTriangle size={18} className="ic" /><span><b>Emergency screen first</b>Warning signs trigger an emergency card and a staff alert before any AI runs.</span></div>
                  <div className="safeguard"><ShieldCheck size={18} className="ic" /><span><b>Your records stay private</b>Shown only after phone, date of birth and a one-time code match.</span></div>
                  <div className="safeguard"><CalendarClock size={18} className="ic" /><span><b>Nothing booked without you</b>Every booking, change or cancellation waits for your Confirm.</span></div>
                  <div className="safeguard"><BookOpen size={18} className="ic" /><span><b>Grounded, never guessed</b>Answers come from clinic documents; unknown questions are declined.</span></div>
                </div>
              </div>
            )}
            {messages.map((m) => (m.role === 'user'
              ? <div className="row" key={m.id}><div className="avatar you">You</div><div><div className="who"><b>You</b><span>{m.time}</span></div><div className="user-note">{m.text}</div></div></div>
              : <AssistantRow key={m.id} m={m} selected={m.id === selected} onSelect={() => setSelected(m.id)} onSend={send} onConfirm={confirm} demo={config?.demo_mode} />))}
            {stages && <Processing stages={stages} />}
          </div>
          {!atBottom && <button className="jump" onClick={() => { setAtBottom(true); scrollDown(true) }}><ArrowDown size={14} /> Latest message</button>}
        </div>
        <Composer onSend={send} busy={busy} models={models} model={model} setModel={setModel} />
      </section>

      {panel && <EvidencePanel res={selectedRes} verified={verified} onBack={() => setSelected(null)} health={health} messages={messages} session={session} title={current?.title} />}
    </div>
  )
}
