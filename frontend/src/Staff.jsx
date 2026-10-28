import React, { useCallback, useEffect, useState } from 'react'
import { Activity, CalendarDays, ClipboardList, FlaskConical, Gauge, Inbox, Lock, LogOut, Play, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import { getJSON, postJSON } from './api.js'
import { POSITIONING } from './brand.js'
import Markdown from './Markdown.jsx'

const STAGE_COLORS = { safety: '#22c55e', understand: '#06b6d4', retrieve: '#4f46e5', safety_llm: '#f59e0b', write: '#7c3aed', records: '#38bdf8', identity: '#a78bfa', guard: '#94a3b8', alert: '#ef4444', write_db: '#10b981', escalate: '#f97316', handoff: '#f97316', screen: '#ef4444' }
const fmtTs = (ts) => (ts || '').replace('T', ' ').slice(0, 16)

function Login({ onLogin }) {
  const [u, setU] = useState('staff')
  const [p, setP] = useState('')
  const [err, setErr] = useState('')
  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    try { onLogin(await postJSON('/api/staff/login', { username: u, password: p })) } catch (ex) { setErr(ex.message) }
  }
  return (
    <div className="login">
      <form onSubmit={submit}>
        <h2><Lock size={20} style={{ verticalAlign: -3 }} /> Staff console</h2>
        <p className="sub" style={{ color: 'var(--text-faint)', margin: '0 0 6px' }}>{POSITIONING}</p>
        <p className="sub" style={{ color: 'var(--text-faint)', margin: 0 }}>Handoff queue, appointments, audit, traces, evals and security.</p>
        <label className="field">Username<input value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" /></label>
        <label className="field">Password<input type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" /></label>
        {err && <div className="error">{err}</div>}
        <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }}>Sign in</button>
        <p style={{ fontSize: 12, color: 'var(--text-faint)', marginTop: 12 }}>Credentials come from STAFF_USERNAME / STAFF_PASSWORD in .env.</p>
      </form>
    </div>
  )
}

function Overview({ token }) {
  const [d, setD] = useState(null)
  useEffect(() => { const load = () => getJSON('/api/staff/overview', token).then(setD).catch(() => {}); load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [token])
  if (!d) return <div className="empty">Loading…</div>
  return (
    <div className="tiles">
      <div className={`tile ${d.urgent_open ? 'alert' : ''}`}><b>{d.urgent_open}</b><span>Urgent emergency alerts open</span></div>
      <div className="tile"><b>{d.open_handoffs}</b><span>Open handoff tickets</span></div>
      <div className="tile"><b>{d.appointments_today}</b><span>Appointments today</span></div>
      <div className="tile"><b>{d.guardrail_events_24h}</b><span>Guardrail events (24 h)</span></div>
      <div className="tile"><b>{d.feedback_down}</b><span>Negative feedback / reports</span></div>
    </div>
  )
}

function Handoffs({ token, onCount }) {
  const [items, setItems] = useState([])
  const load = useCallback(() => getJSON('/api/staff/handoffs', token).then((d) => { setItems(d.items); onCount(d.items.filter((i) => i.status === 'open').length) }).catch(() => {}), [token, onCount])
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t) }, [load])
  const set = async (id, status) => { await postJSON(`/api/staff/handoffs/${id}`, { status }, token); load() }
  if (!items.length) return <div className="empty">The queue is empty.</div>
  return (
    <table className="table">
      <thead><tr><th>#</th><th>Priority</th><th>Kind</th><th>Summary</th><th>Patient</th><th>Created</th><th>Status</th><th /></tr></thead>
      <tbody>{items.map((h) => (
        <tr key={h.id}>
          <td>{h.id}</td><td><span className={`prio ${h.priority}`}>{h.priority}</span></td><td>{h.kind}</td>
          <td className="mono" style={{ maxWidth: 420 }}>{h.summary}</td><td>{h.patient_name || <span style={{ color: 'var(--text-faint)' }}>unverified</span>}</td>
          <td>{fmtTs(h.created_at)}</td><td>{h.status}</td>
          <td style={{ whiteSpace: 'nowrap' }}>
            {h.status === 'open' && <button className="btn" onClick={() => set(h.id, 'claimed')}>Claim</button>}{' '}
            {h.status !== 'closed' && <button className="btn ghost" onClick={() => set(h.id, 'closed')}>Close</button>}
          </td>
        </tr>))}
      </tbody>
    </table>
  )
}

function Appointments({ token }) {
  const [d, setD] = useState(null)
  useEffect(() => { getJSON('/api/staff/appointments', token).then(setD).catch(() => {}) }, [token])
  if (!d) return <div className="empty">Loading…</div>
  return (
    <>
      <h3 style={{ margin: '0 0 10px' }}>Today ({d.day})</h3>
      {d.items.length ? (
        <table className="table"><thead><tr><th>Time</th><th>Patient</th><th>Doctor</th><th>Room</th><th>Reason</th><th>Status</th></tr></thead>
          <tbody>{d.items.map((a) => <tr key={a.id}><td>{a.start_at.slice(11)}</td><td>{a.patient_name}</td><td>{a.doctor}</td><td>{a.room}</td><td>{a.reason}</td><td>{a.status}</td></tr>)}</tbody></table>
      ) : <div className="empty">No appointments today.</div>}
      <h3 style={{ margin: '22px 0 10px' }}>Next booked</h3>
      <table className="table"><thead><tr><th>When</th><th>Patient</th><th>Doctor</th><th>Ref</th></tr></thead>
        <tbody>{d.upcoming.map((a) => <tr key={a.id}><td>{fmtTs(a.start_at)}</td><td>{a.patient_name}</td><td>{a.doctor}</td><td>#{a.id}</td></tr>)}</tbody></table>
    </>
  )
}

function Audit({ token }) {
  const [actor, setActor] = useState('')
  const [items, setItems] = useState([])
  useEffect(() => { getJSON(`/api/staff/audit?limit=300${actor ? `&actor=${actor}` : ''}`, token).then((d) => setItems(d.items)).catch(() => {}) }, [token, actor])
  return (
    <>
      <div className="toolbar2"><div className="seg">{['', 'guardrail', 'retriever', 'tool', 'staff', 'patient'].map((a) => <button key={a} className={actor === a ? 'on' : ''} onClick={() => setActor(a)}>{a || 'all'}</button>)}</div>
        <span style={{ color: 'var(--text-faint)', fontSize: 13 }}>Personal data is masked; retrievals log document ids only.</span></div>
      <table className="table"><thead><tr><th>Time</th><th>Actor</th><th>Event</th><th>Session</th><th>Detail</th></tr></thead>
        <tbody>{items.map((r) => <tr key={r.id}><td style={{ whiteSpace: 'nowrap' }}>{fmtTs(r.ts)}</td><td>{r.actor}</td><td>{r.event}</td><td className="mono">{r.session_id}</td><td className="mono">{JSON.stringify(r.detail)}</td></tr>)}</tbody></table>
    </>
  )
}

function Traces({ token }) {
  const [items, setItems] = useState([])
  const load = () => getJSON('/api/staff/traces?limit=80', token).then((d) => setItems(d.items)).catch(() => {})
  useEffect(() => { load() }, [token]) // eslint-disable-line react-hooks/exhaustive-deps
  const keys = [...new Set(items.flatMap((t) => (t.stages || []).map((s) => s.key)))]
  return (
    <>
      <div className="toolbar2"><button className="btn" onClick={load}><RefreshCw size={14} /> Refresh</button></div>
      <div className="legend">{keys.map((k) => <span key={k}><i style={{ background: STAGE_COLORS[k] || '#64748b' }} />{k}</span>)}</div>
      <table className="table"><thead><tr><th>Time</th><th>Message (masked)</th><th>Intent</th><th>Route</th><th>Stages</th><th>Total</th><th>Tokens</th><th>Tools</th></tr></thead>
        <tbody>{items.map((t) => {
          const total = Math.max(1, (t.stages || []).reduce((a, s) => a + s.ms, 0))
          const tok = (t.llm_usage || []).reduce((a, u) => a + (u.in || 0) + (u.out || 0), 0)
          return (
            <tr key={t.turn_id}>
              <td style={{ whiteSpace: 'nowrap' }}>{t.ts?.slice(11)}</td><td style={{ maxWidth: 260 }}>{t.text}</td><td>{t.intent}</td><td className="mono">{t.route_source}</td>
              <td><div className="bar-track" title={(t.stages || []).map((s) => `${s.label}: ${Math.round(s.ms)}ms`).join('\n')}>
                {(t.stages || []).map((s, i) => <i key={i} style={{ width: `${(s.ms / total) * 100}%`, background: STAGE_COLORS[s.key] || '#64748b' }} />)}</div></td>
              <td style={{ whiteSpace: 'nowrap' }}>{(t.total_ms / 1000).toFixed(2)}s</td><td>{tok || '-'}</td><td className="mono">{(t.tool_calls || []).map((c) => c.tool).join(', ')}</td>
            </tr>
          )
        })}</tbody></table>
    </>
  )
}

function Evals({ token }) {
  const [d, setD] = useState(null)
  const load = useCallback(() => getJSON('/api/staff/evals', token).then(setD).catch(() => {}), [token])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])
  const run = async () => { await postJSON('/api/staff/evals/run', {}, token).catch((e) => alert(e.message)); load() }
  return (
    <>
      <div className="toolbar2">
        <button className="btn primary" onClick={run} disabled={d?.running}><Play size={15} /> {d?.running ? 'Running evals…' : 'Run evals'}</button>
        {d?.report_name && <span style={{ color: 'var(--text-faint)', fontSize: 13 }}>Latest: {d.report_name}</span>}
      </div>
      {d?.summary && (
        <div className="tiles">{d.summary.suites.map((s) => (
          <div className={`tile ${s.passed ? '' : 'alert'}`} key={s.name}><b>{s.score_display}</b><span>{s.name} · target {s.target_display} · {s.passed ? 'PASS' : 'FAIL'}</span></div>))}
        </div>
      )}
      {d?.report ? <div className="report"><Markdown text={d.report} /></div> : <div className="empty">No eval report yet - press Run evals.</div>}
    </>
  )
}

function Sops({ token }) {
  const [q, setQ] = useState('emergency alert ticket')
  const [res, setRes] = useState([])
  const search = async (e) => { e?.preventDefault(); setRes((await getJSON(`/api/staff/sops?q=${encodeURIComponent(q)}`, token)).results) }
  useEffect(() => { search() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <form className="toolbar2" onSubmit={search}><input className="search" style={{ flex: 1 }} value={q} onChange={(e) => setQ(e.target.value)} /><button className="btn"><Search size={14} /> Search</button></form>
      <p className="sub">Staff-only documents. The same search from a patient session returns nothing (role-based access control before retrieval).</p>
      {res.map((r) => <div className="report" style={{ marginBottom: 10 }} key={r.node_id}><b>{r.title} — {r.section}</b><Markdown text={r.text} /></div>)}
    </>
  )
}

function Security({ token }) {
  const [d, setD] = useState(null)
  useEffect(() => { getJSON('/api/staff/security', token).then(setD).catch(() => {}) }, [token])
  if (!d) return <div className="empty">Loading security status…</div>
  const ev = d.latest_security_eval
  const st = d.settings
  const flags = [
    ['Signing secret configured', st.session_secret_set], ['Staff password changed from default', st.staff_password_changed],
    [`Verified sessions expire after ${st.verified_ttl_minutes} min`, st.verified_ttl_minutes > 0], [`Saved chats deleted after ${st.chat_retention_days} days`, st.chat_retention_days > 0],
    ['Demo mode off (real OTP delivery)', !st.demo_mode],
  ]
  return (
    <>
      <div className="tiles">
        <div className={`tile ${ev && !ev.passed ? 'alert' : ''}`}><b>{ev ? (ev.passed ? 'PASS' : 'FAIL') : '–'}</b><span>Security eval{ev ? ` · ${ev.score_display} leaks · target ${ev.target_display}` : ' · run evals'}</span></div>
        <div className="tile"><b>{d.layers[0].evidence.blocked_total}</b><span>Queries blocked (layer 1)</span></div>
        <div className="tile"><b>{d.guardrails.red_flags}</b><span>Emergency red flags</span></div>
        <div className="tile"><b>{d.guardrails.output_blocked}</b><span>Unsafe answers replaced</span></div>
        <div className={`tile ${d.layers[3].evidence.raw_pii_found ? 'alert' : ''}`}><b>{d.layers[3].evidence.raw_pii_found}</b><span>Raw PII in last {d.layers[3].evidence.recent_rows_scanned} audit rows</span></div>
        <div className="tile"><b>{d.guardrails.identity_lockouts}</b><span>Phone numbers locked (brute force)</span></div>
      </div>
      {d.layers.map((l) => (
        <div className="report" key={l.id} style={{ marginBottom: 10 }}>
          <b>Layer {l.id}: {l.title}</b>
          <ul style={{ margin: '6px 0' }}>{l.controls.map((c) => <li key={c}>{c}</li>)}</ul>
          <div className="sub" style={{ fontFamily: 'var(--mono)', fontSize: 12, whiteSpace: 'pre-wrap' }}>
            {l.id === 1 && (l.evidence.by_reason.length ? l.evidence.by_reason.map((b) => `${b.reason}: ${b.n}`).join(' · ') : 'No blocked queries yet.')}
            {l.id === 2 && `Retrievals by role: ${l.evidence.retrievals_by_role.map((r) => `${r.role} ${r.n}`).join(' · ') || 'none yet'}\nStaff-only documents: ${l.evidence.staff_only_docs} · other-tenant documents (never returned): ${l.evidence.other_tenant_docs}`}
            {l.id === 3 && l.evidence.templates.map((t) => `${t.name}: ${Object.entries(t.slots).map(([k, v]) => `${k}≤${v}`).join(', ')}`).join('\n')}
            {l.id === 4 && `Audit rows: ${l.evidence.audit_rows} · scanned: ${l.evidence.recent_rows_scanned} · raw PII found: ${l.evidence.raw_pii_found}`}
          </div>
        </div>
      ))}
      <div className="report">
        <b>Deployment settings</b>
        <ul style={{ margin: '6px 0' }}>{flags.map(([label, ok]) => <li key={label}>{ok ? '✓' : '⚠'} {label}</li>)}</ul>
        <div className="sub">Demo mode shows the one-time code on screen because no SMS gateway is connected; brute force is still blocked per phone number.</div>
      </div>
    </>
  )
}

const TABS = [
  ['overview', 'Overview', Gauge], ['queue', 'Handoff queue', Inbox], ['appointments', 'Appointments', CalendarDays], ['audit', 'Audit log', ClipboardList],
  ['traces', 'Traces', Activity], ['evals', 'Evals', FlaskConical], ['security', 'Security', ShieldCheck], ['sops', 'Staff SOPs', Lock],
]

export default function Staff() {
  // The session lives in an HttpOnly cookie set by the server: JavaScript never sees the token.
  const [auth, setAuth] = useState(undefined)
  const [tab, setTab] = useState('overview')
  const [openCount, setOpenCount] = useState(0)
  useEffect(() => { getJSON('/api/staff/me').then(setAuth).catch(() => setAuth(null)) }, [])
  const login = (a) => setAuth({ username: a.username })
  const logout = async () => { await postJSON('/api/staff/logout', {}).catch(() => {}); setAuth(null) }
  if (auth === undefined) return <div className="empty">Checking staff session…</div>
  if (!auth) return <Login onLogin={login} />
  const t = null
  const title = TABS.find((x) => x[0] === tab)[1]
  return (
    <div className="console">
      <nav className="side">
        {TABS.map(([id, label, Icon]) => (
          <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}><Icon size={17} /> {label}{id === 'queue' && openCount > 0 && <span className="count">{openCount}</span>}</button>
        ))}
        <div style={{ flex: 1 }} />
        <button onClick={logout}><LogOut size={17} /> Sign out ({auth.username})</button>
      </nav>
      <main className="main">
        <h2>{title}</h2>
        <p className="sub">{{ overview: 'Live view of the AI front desk.', queue: 'Emergency alerts, pharmacist callbacks and human handoffs.', appointments: 'Bookings made through CareHarbor AI and the front desk.', audit: 'Every tool call, retrieval, guardrail hit and staff action.', traces: 'Per-turn pipeline stages and latency.', evals: 'Safety and accuracy eval suites with pass thresholds.', security: 'The four RAG security layers with live evidence, identity protection and deployment settings.', sops: 'Internal procedures for staff.' }[tab]}</p>
        {tab === 'overview' && (<><Overview token={t} /><h3 style={{ margin: '6px 0 10px' }}>Handoff queue</h3><Handoffs token={t} onCount={setOpenCount} /></>)}
        {tab === 'queue' && <Handoffs token={t} onCount={setOpenCount} />}
        {tab === 'appointments' && <Appointments token={t} />}
        {tab === 'audit' && <Audit token={t} />}
        {tab === 'traces' && <Traces token={t} />}
        {tab === 'evals' && <Evals token={t} />}
        {tab === 'security' && <Security token={t} />}
        {tab === 'sops' && <Sops token={t} />}
      </main>
    </div>
  )
}
