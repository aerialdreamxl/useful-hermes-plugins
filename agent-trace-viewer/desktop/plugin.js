/**
 * Agent Trace Viewer — desktop page.
 *
 * Restores DeepSeek-Harness-style agent traces: a session picker on the left,
 * a chronological OpenAI-messages timeline on the right, with reasoning (CoT)
 * folded by default and a one-click `messages[]` JSON export.
 *
 * Data flows through this plugin's own backend namespace:
 *   ctx.rest('/sessions')                 -> list
 *   ctx.rest('/sessions/:id/messages')    -> raw trace rows
 *   ctx.rest('/sessions/:id/export')      -> OpenAI messages[] payload
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Imports limited to @hermes/plugin-sdk, react, react/jsx-runtime.
 * All data fetching happens in event handlers / useEffect (never in render).
 */

import {
  Button,
  cn,
  EmptyState,
  ErrorState,
  GlyphSpinner,
  ROUTES_AREA,
  ScrollArea,
  SearchField,
  SIDEBAR_NAV_AREA,
  host
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'agent-trace-viewer'

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

function shortTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 19).replace('T', ' ')
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function prettyArgs(raw) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function truncate(text, max) {
  if (text == null) return ''
  const s = String(text)
  return s.length > max ? `${s.slice(0, max)}…` : s
}

// ---------------------------------------------------------------------------
// message cards
// ---------------------------------------------------------------------------

function RoleBadge({ role }) {
  const tone =
    role === 'user'
      ? 'var(--ui-accent)'
      : role === 'assistant'
        ? 'var(--ui-text-secondary)'
        : role === 'tool'
          ? 'var(--ui-text-tertiary)'
          : 'var(--ui-text-quaternary)'
  return jsx(
    'span',
    {
      className:
        'rounded px-1.5 py-0.5 font-mono text-[0.625rem] uppercase tracking-wide border',
      style: { color: tone, borderColor: 'var(--ui-stroke-secondary)' },
      children: role
    }
  )
}

function Foldable({ label, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return jsxs('div', {
    className: 'mt-1.5',
    children: [
      jsx('button', {
        type: 'button',
        onClick: () => setOpen(o => !o),
        className:
          'flex items-center gap-1 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-foreground cursor-pointer select-none',
        children: [
          jsx('span', {
            className: 'inline-block transition-transform',
            style: { transform: open ? 'rotate(90deg)' : 'none' },
            children: '▸'
          }),
          label
        ]
      }),
      open
        ? jsx('div', {
            className:
              'mt-1 rounded border border-(--ui-stroke-secondary) p-2 text-[0.75rem] whitespace-pre-wrap break-words',
            children
          })
        : null
    ]
  })
}

function ToolCallView({ call }) {
  const fn = (call && call.function) || {}
  return jsxs('div', {
    className:
      'mt-1 rounded border border-(--ui-stroke-secondary) p-2 text-[0.75rem]',
    children: [
      jsx('div', {
        className: 'font-mono text-(--ui-accent)',
        children: `⚙ ${fn.name || '(unnamed)'}`
      }),
      jsx('pre', {
        className:
          'mt-1 overflow-x-auto text-[0.6875rem] text-(--ui-text-secondary) whitespace-pre-wrap break-all',
        children: truncate(prettyArgs(fn.arguments), 4000)
      })
    ]
  })
}

function MessageCard({ m }) {
  const isCompacted = Boolean(m.compacted && !m.active)
  const children = []

  // header row
  children.push(
    jsxs('div', {
      className: 'flex items-center gap-2',
      children: [
        jsx(RoleBadge, { role: m.role }),
        m.tool_name
          ? jsx('span', {
              className: 'font-mono text-[0.6875rem] text-(--ui-text-tertiary)',
              children: m.tool_name
            })
          : null,
        m.timestamp
          ? jsx('span', {
              className: 'ml-auto text-[0.625rem] text-(--ui-text-quaternary)',
              children: shortTime(m.timestamp)
            })
          : null
      ]
    })
  )

  if (isCompacted) {
    children.push(
      jsx('div', {
        className: 'mt-1 text-[0.6875rem] italic text-(--ui-text-quaternary)',
        children: '— compacted by context compression (original row kept in store) —'
      })
    )
  }

  // reasoning (CoT) — folded by default per user preference
  if (m.reasoning) {
    children.push(
      jsx(Foldable, {
        label: `reasoning · ${m.reasoning.length} chars`,
        children: m.reasoning
      })
    )
  }

  // assistant tool_calls
  if (m.role === 'assistant' && Array.isArray(m.tool_calls)) {
    for (const [i, c] of m.tool_calls.entries()) {
      children.push(jsx(ToolCallView, { call: c }, i))
    }
  }

  // content — long bodies get folded, short ones inline
  if (m.content != null && m.content !== '') {
    const long = String(m.content).length > 1500
    children.push(
      long
        ? jsx(Foldable, {
            label: `content · ${String(m.content).length} chars`,
            children: m.content
          })
        : jsx('div', {
            className: 'mt-1 text-[0.8125rem] whitespace-pre-wrap break-words',
            children: m.content
          })
    )
  }

  return jsx('div', {
    className: cn(
      'rounded-md border border-(--ui-stroke-secondary) p-2.5',
      isCompacted && 'opacity-60'
    ),
    children
  })
}

// ---------------------------------------------------------------------------
// trace timeline — data fetched in useEffect keyed on [sessionId, order]
// ---------------------------------------------------------------------------

function TracePane({ sessionId, rest }) {
  const [order, setOrder] = useState('latest')
  const [state, setState] = useState({ data: null, error: null, loading: false })

  useEffect(() => {
    if (!sessionId) return undefined
    let alive = true
    setState({ data: null, error: null, loading: true })
    rest(
      `/sessions/${encodeURIComponent(sessionId)}/messages?limit=300&order=${order}`
    )
      .then(d => {
        if (alive) setState({ data: d, error: null, loading: false })
      })
      .catch(e => {
        if (alive)
          setState({ data: null, error: String((e && e.message) || e), loading: false })
      })
    return () => {
      alive = false
    }
  }, [sessionId, order, rest])

  if (!sessionId) {
    return jsx(EmptyState, {
      title: 'No session selected',
      description: 'Pick a session from the list to inspect its trace.'
    })
  }
  if (state.loading) return jsx(GlyphSpinner, {})
  if (state.error) {
    return jsx(ErrorState, { title: 'Failed to load trace', description: state.error })
  }

  const msgs = (state.data && state.data.messages) || []

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className:
          'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [
          jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: `${msgs.length} rows`
          }),
          jsx('div', {
            className: 'ml-auto flex gap-1',
            children: [
              jsx(Button, {
                variant: order === 'latest' ? 'secondary' : 'ghost',
                size: 'xs',
                onClick: () => setOrder('latest'),
                children: 'latest 300'
              }),
              jsx(Button, {
                variant: order === 'oldest' ? 'secondary' : 'ghost',
                size: 'xs',
                onClick: () => setOrder('oldest'),
                children: 'from start'
              })
            ]
          })
        ]
      }),
      jsx(ScrollArea, {
        className: 'flex-1',
        children: jsx('div', {
          className: 'flex flex-col gap-2 p-3',
          children: msgs.map(m => jsx(MessageCard, { m }, m.row_id))
        })
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// export
// ---------------------------------------------------------------------------

async function exportSession(sessionId, rest) {
  const data = await rest(`/sessions/${encodeURIComponent(sessionId)}/export`)
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: 'application/json'
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `trace_${sessionId}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
  return data.meta && data.meta.message_count
}

// ---------------------------------------------------------------------------
// page — sessions loaded in useEffect, search via event handler
// ---------------------------------------------------------------------------

function TracePage({ ctx }) {
  const [q, setQ] = useState('')
  const [sel, setSel] = useState('')
  const [sessions, setSessions] = useState({ list: [], error: null, loading: true })
  const [exporting, setExporting] = useState(false)

  const loadSessions = query => {
    setSessions(s => ({ ...s, loading: true }))
    ctx
      .rest(`/sessions?limit=100&q=${encodeURIComponent(query || '')}`)
      .then(d => setSessions({ list: d.sessions || [], error: null, loading: false }))
      .catch(e =>
        setSessions({ list: [], error: String((e && e.message) || e), loading: false })
      )
  }

  // initial load, exactly once
  useEffect(() => {
    loadSessions('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return jsxs('div', {
    className: 'flex h-full',
    children: [
      // left: session list
      jsxs('div', {
        className: 'flex w-72 shrink-0 flex-col border-r border-(--ui-stroke-secondary)',
        children: [
          jsx('div', {
            className: 'p-2',
            children: jsx(SearchField, {
              value: q,
              placeholder: 'Search sessions…',
              onChange: v => {
                setQ(v)
                loadSessions(v)
              }
            })
          }),
          sessions.error
            ? jsx(ErrorState, {
                title: 'Backend unavailable',
                description: sessions.error
              })
            : jsx(ScrollArea, {
                className: 'flex-1',
                children: jsx('div', {
                  className: 'flex flex-col',
                  children: sessions.list.map(s =>
                    jsx(
                      'button',
                      {
                        type: 'button',
                        onClick: () => setSel(s.id),
                        className: cn(
                          'flex flex-col gap-0.5 border-b border-(--ui-stroke-secondary)/60 px-2.5 py-2 text-left hover:bg-(--chrome-action-hover)',
                          sel === s.id && 'bg-(--chrome-action-hover)'
                        ),
                        children: [
                          jsx('span', {
                            className: 'truncate text-[0.75rem]',
                            children: s.title
                          }),
                          jsxs('span', {
                            className:
                              'flex items-center gap-1.5 text-[0.625rem] text-(--ui-text-quaternary)',
                            children: [
                              jsx('span', { children: `${s.message_count ?? 0} msgs` }),
                              jsx('span', {
                                children: shortTime(s.last_activity_at || s.started_at)
                              }),
                              s.model ? jsx('span', { className: 'truncate', children: s.model }) : null
                            ]
                          })
                        ]
                      },
                      s.id
                    )
                  )
                })
              })
        ]
      }),
      // right: trace timeline
      jsxs('div', {
        className: 'flex min-w-0 flex-1 flex-col',
        children: [
          sel
            ? jsxs('div', {
                className:
                  'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2',
                children: [
                  jsx('div', {
                    className: 'min-w-0 truncate text-[0.8125rem] font-medium',
                    children: sel
                  }),
                  jsx(Button, {
                    size: 'sm',
                    disabled: exporting,
                    onClick: () => {
                      setExporting(true)
                      exportSession(sel, ctx.rest)
                        .then(n =>
                          host.notify({
                            kind: 'success',
                            message: `Exported ${n} messages → trace_${sel}.json`
                          })
                        )
                        .catch(e =>
                          host.notify({
                            kind: 'error',
                            message: `Export failed: ${(e && e.message) || e}`
                          })
                        )
                        .finally(() => setExporting(false))
                    },
                    children: exporting ? 'Exporting…' : '⬇ Export messages[] JSON'
                  })
                ]
              })
            : null,
          jsx('div', {
            className: 'min-h-0 flex-1',
            children: jsx(TracePane, { sessionId: sel, rest: ctx.rest })
          })
        ]
      })
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Agent Trace Viewer',
  defaultEnabled: true,
  register(ctx) {
    // Full page in the workspace pane.
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/agent-traces' },
      render: () => jsx(TracePage, { ctx })
    })

    // Sidebar navigation row.
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: '/agent-traces', label: 'Agent Traces', codicon: 'list-tree' }
    })
  }
}
