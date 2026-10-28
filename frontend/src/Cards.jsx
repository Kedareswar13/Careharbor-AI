// Special answer cards: emergency, confirmation (drives the LangGraph interrupt), slots, OTP, triage, prescriptions...
import React from 'react'
import { AlertTriangle, CalendarCheck, CalendarClock, CheckCircle2, KeyRound, Phone, Pill, ShieldCheck, Stethoscope, Ticket, UserRound } from 'lucide-react'

const DISPO = { self_care: 'Self-care at home', book: 'Book a visit soon', urgent: 'See a doctor today', emergency: 'Emergency' }

export default function Card({ card, answered, onSend, onConfirm, demo }) {
  if (!card) return null
  switch (card.type) {
    case 'emergency':
      return (
        <div className="card emergency" role="alert">
          <h4><AlertTriangle size={20} /> Call emergency services now</h4>
          <p>Go to the nearest emergency department or call an ambulance. Our clinic staff have been alerted{card.ticket_id ? ` (ticket #${card.ticket_id})` : ''}.</p>
          <div className="row-btns">
            <a className="btn danger call" href="tel:112"><Phone size={18} /> Call 112</a>
            <a className="btn danger call" href="tel:108"><Phone size={18} /> Ambulance 108</a>
            {card.crisis_line && <a className="btn" href="tel:14416"><Phone size={16} /> Tele-MANAS 14416</a>}
          </div>
        </div>
      )
    case 'confirm': {
      const title = { book: 'Confirm booking', reschedule: 'Confirm new time', cancel: 'Confirm cancellation' }[card.action] || 'Please confirm'
      return (
        <div className="card confirm">
          <h4><CalendarCheck size={18} /> {title}</h4>
          <p>Nothing is saved until you press Confirm.</p>
          <div className="kv">
            <span>Doctor</span><span>{card.doctor}{card.specialty ? ` · ${card.specialty}` : ''}</span>
            {card.previous && (<><span>Currently</span><span>{card.previous}</span></>)}
            <span>{card.action === 'cancel' ? 'Appointment' : 'New time'}</span><span><b>{card.when}</b></span>
            {card.reason && card.action !== 'cancel' && (<><span>Reason</span><span>{card.reason}</span></>)}
          </div>
          <div className="row-btns">
            <button className="btn primary" disabled={answered} onClick={() => onConfirm('confirm')}><CheckCircle2 size={16} /> Confirm</button>
            {card.action !== 'cancel' && <button className="btn" disabled={answered} onClick={() => onConfirm('change')}><CalendarClock size={16} /> Change</button>}
            <button className="btn ghost" disabled={answered} onClick={() => onConfirm('cancel')}>{card.action === 'cancel' ? 'Keep it' : 'Cancel'}</button>
          </div>
        </div>
      )
    }
    case 'slots':
      return (
        <div className="card">
          <h4><CalendarClock size={18} /> Available with {card.doctor}</h4>
          <div className="slots">
            {card.options.map((o) => (
              <button key={o.slot_id} className="slot" disabled={answered} onClick={() => onSend(String(o.n))}>
                <b>{o.label}</b><small>Option {o.n}</small>
              </button>
            ))}
          </div>
          <div className="quick"><button disabled={answered} onClick={() => onSend('show other times')}>Other times</button></div>
        </div>
      )
    case 'appointments':
      return (
        <div className="card">
          <h4><CalendarClock size={18} /> Your upcoming appointments</h4>
          <div className="slots">
            {card.options.map((a, i) => (
              <button key={a.appointment_id} className="slot" disabled={answered} onClick={() => onSend(String(i + 1))}>
                <b>{a.doctor}</b><small>{a.start_at.replace('T', ' ')}</small>
              </button>
            ))}
          </div>
        </div>
      )
    case 'otp':
      return (
        <div className="card">
          <h4><KeyRound size={18} /> One-time code sent to {card.sent_to}</h4>
          {card.demo_otp ? (
            <>
              <p>Demo mode shows the code here instead of sending an SMS.</p>
              <div className="row-btns" style={{ alignItems: 'center' }}>
                <span className="otp">{card.demo_otp}</span>
                <button className="btn primary" disabled={answered} onClick={() => onSend(card.demo_otp)}>Use this code</button>
              </div>
            </>
          ) : <p>Type the 6-digit code you received.</p>}
        </div>
      )
    case 'identity':
      return (
        <div className="card">
          <h4><ShieldCheck size={18} /> Verify it's you</h4>
          <p>Your records stay private: I only show them after your phone number, date of birth and a one-time code match.</p>
          {demo && (
            <div className="quick">
              <button disabled={answered} onClick={() => onSend('9876500001, 14/05/1990')}><UserRound size={13} /> Demo: Aarav Sharma</button>
              <button disabled={answered} onClick={() => onSend('9876500002, 02/11/1985')}><UserRound size={13} /> Demo: Ananya Iyer</button>
              <button disabled={answered} onClick={() => onSend('9000000001, 01/01/2000')}><UserRound size={13} /> QA test account</button>
            </div>
          )}
        </div>
      )
    case 'followup':
      return (
        <div className="quick">
          <button disabled={answered} onClick={() => onSend('No, none of these. It started recently and is mild.')}>No, none of these</button>
          <button disabled={answered} onClick={() => onSend('Yes')}>Yes, I have one of these</button>
        </div>
      )
    case 'triage':
      return (
        <div className="card">
          <h4><Stethoscope size={18} /> Guidance summary</h4>
          <div className="row-btns" style={{ marginTop: 6, alignItems: 'center' }}>
            <span className={`pill ${card.disposition}`}>{DISPO[card.disposition] || card.disposition}</span>
            <span style={{ color: 'var(--text-faint)', fontSize: 13 }}>Suggested department: {card.department}</span>
          </div>
          <div className="row-btns">
            <button className="btn primary" disabled={answered} onClick={() => onSend('Yes, please book an appointment')}><CalendarCheck size={16} /> Book with {card.department}</button>
            <button className="btn ghost" disabled={answered} onClick={() => onSend('No thanks')}>Not now</button>
          </div>
        </div>
      )
    case 'prescriptions':
      return (
        <div className="card">
          <h4><Pill size={18} /> As recorded by your doctor</h4>
          <div className="rx">
            {card.items.map((p, i) => (
              <div className="rx-item" key={i}>
                <b>{p.drug} {p.strength}</b> <span style={{ color: 'var(--text-faint)' }}>· {p.form}</span>
                <div className="sched">{p.dose} · {p.frequency} · {p.times}{p.food !== 'not applicable' ? ` · ${p.food}` : ''}</div>
                <div className="sched">{p.duration_days >= 90 ? 'Long-term' : `${p.duration_days} days`} · {p.start_date} → {p.end_date} · {p.doctor}</div>
              </div>
            ))}
          </div>
        </div>
      )
    case 'result':
      if (!card.ok || !card.appointment) return null
      return (
        <div className="card">
          <h4><CheckCircle2 size={18} color="var(--ok)" /> {card.action === 'cancel' ? 'Cancelled' : 'Saved in the clinic system'}</h4>
          <div className="kv">
            <span>Reference</span><span>#{card.appointment.appointment_id}</span>
            <span>Doctor</span><span>{card.appointment.doctor} · {card.appointment.specialty}</span>
            <span>When</span><span>{card.appointment.start_at.replace('T', ' ')}</span>
            <span>Room</span><span>{card.appointment.room}</span>
            <span>Status</span><span>{card.appointment.status}</span>
          </div>
        </div>
      )
    case 'handoff':
      return (
        <div className="card">
          <h4><Ticket size={18} /> {card.kind === 'pharmacist' ? 'Pharmacist will contact you' : 'Passed to our team'}</h4>
          <p>Ticket #{card.ticket_id} is in the staff queue.</p>
        </div>
      )
    default:
      return null
  }
}
