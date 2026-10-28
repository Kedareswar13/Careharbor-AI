// Minimal, safe Markdown renderer (bold, bullet/numbered lists, tables, headings, paragraphs).
// Builds React elements - never injects HTML - so model or document text can't inject markup.
import React from 'react'

function inline(text, key) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((p, i) => (p.startsWith('**') && p.endsWith('**') ? <strong key={`${key}-${i}`}>{p.slice(2, -2)}</strong> : <React.Fragment key={`${key}-${i}`}>{p}</React.Fragment>))
}

export default function Markdown({ text }) {
  const lines = (text || '').split('\n')
  const out = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const start = i
    if (/^\s*[-*]\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ''))
      out.push(<ul key={`ul${start}`}>{items.map((t, j) => <li key={j}>{inline(t, `${start}-${j}`)}</li>)}</ul>)
      continue
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+\.\s+/, ''))
      out.push(<ol key={`ol${start}`}>{items.map((t, j) => <li key={j}>{inline(t, `${start}-${j}`)}</li>)}</ol>)
      continue
    }
    if (/^\s*\|/.test(line)) {
      const rows = []
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++])
      const cells = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r)).map((r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim()))
      out.push(
        <table key={`t${start}`}><thead><tr>{cells[0].map((c, j) => <th key={j}>{inline(c, `h${j}`)}</th>)}</tr></thead>
          <tbody>{cells.slice(1).map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c, `${k}-${j}`)}</td>)}</tr>)}</tbody></table>,
      )
      continue
    }
    const h = line.match(/^(#{1,3})\s+(.*)/)
    if (h) {
      const Tag = `h${Math.min(h[1].length + 2, 5)}`
      out.push(<Tag key={`h${start}`}>{inline(h[2], `h${i}`)}</Tag>)
      i++
      continue
    }
    if (line.trim()) out.push(<p key={`p${start}`}>{inline(line, `p${i}`)}</p>)
    i++
  }
  return <div className="text">{out}</div>
}
