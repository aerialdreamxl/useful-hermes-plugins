/**
 * Agent Trace Viewer — dashboard entry (classic script, NOT ESM).
 *
 * Contract (web/src/plugins/registry.ts):
 *   - loaded as a plain <script src="/dashboard-plugins/<name>/<entry>">
 *   - must call window.__HERMES_PLUGINS__.register(name, ReactComponent)
 *   - only window.__HERMES_PLUGIN_SDK__ globals are available (host React)
 *
 * Renders a read-only trace viewer backed by this plugin's API:
 *   GET /api/plugins/agent-trace-viewer/sessions
 *   GET /api/plugins/agent-trace-viewer/sessions/{id}/messages
 *   GET /api/plugins/agent-trace-viewer/sessions/{id}/export
 */
(function () {
  'use strict'

  var sdk = window.__HERMES_PLUGIN_SDK__
  if (!sdk || !window.__HERMES_PLUGINS__) return

  var React = sdk.React
  var useState = sdk.hooks.useState
  var useEffect = sdk.hooks.useEffect
  var fetchJSON = sdk.fetchJSON
  var e = React.createElement
  var API = '/api/plugins/agent-trace-viewer'

  // ---------------------------------------------------------------- helpers
  function shortTime(iso) {
    if (!iso) return ''
    var d = new Date(iso)
    if (isNaN(d.getTime())) return String(iso).slice(0, 19).replace('T', ' ')
    function p(n) { return String(n).padStart(2, '0') }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes())
  }

  function prettyArgs(raw) {
    try { return JSON.stringify(JSON.parse(raw), null, 2) } catch (_err) { return raw }
  }

  function truncate(text, max) {
    if (text == null) return ''
    var s = String(text)
    return s.length > max ? s.slice(0, max) + '…' : s
  }

  var baseStyle = {
    fontFamily: 'inherit',
    fontSize: '13px',
    color: 'var(--foreground, inherit)'
  }

  // ------------------------------------------------------------ Foldable row
  function Foldable(props) {
    var openState = useState(false)
    var open = openState[0]
    var setOpen = openState[1]
    return e('div', { style: { marginTop: 6 } },
      e('button', {
        type: 'button',
        onClick: function () { setOpen(!open) },
        style: {
          background: 'none', border: 'none', cursor: 'pointer', padding: 0,
          fontSize: '12px', color: 'var(--muted-foreground, #888)', fontFamily: 'inherit'
        }
      }, (open ? '▾ ' : '▸ ') + props.label),
      open ? e('div', {
        style: {
          marginTop: 4, padding: 8, fontSize: '12px', whiteSpace: 'pre-wrap',
          wordBreak: 'break-word', border: '1px solid var(--border, #ddd)',
          borderRadius: 6, background: 'var(--card, rgba(0,0,0,0.03))'
        }
      }, props.children) : null
    )
  }

  // ------------------------------------------------------------ Message card
  var roleColors = {
    user: '#2563eb', assistant: '#7c3aed', tool: '#0f766e', system: '#b45309'
  }

  function MessageCard(props) {
    var m = props.m
    var isCompacted = Boolean(m.compacted && !m.active)
    var children = []

    // header
    children.push(e('div', { key: 'h', style: { display: 'flex', gap: 8, alignItems: 'center' } },
      e('span', {
        style: {
          fontFamily: 'monospace', fontSize: '10px', textTransform: 'uppercase',
          letterSpacing: '0.05em', padding: '1px 6px', borderRadius: 4,
          border: '1px solid var(--border, #ccc)', color: roleColors[m.role] || '#666'
        }
      }, m.role),
      m.tool_name ? e('span', { style: { fontFamily: 'monospace', fontSize: '11px', color: 'var(--muted-foreground, #888)' } }, m.tool_name) : null,
      m.timestamp ? e('span', { style: { marginLeft: 'auto', fontSize: '10px', color: 'var(--muted-foreground, #999)' } }, shortTime(m.timestamp)) : null
    ))

    if (isCompacted) {
      children.push(e('div', {
        key: 'c',
        style: { marginTop: 4, fontSize: '11px', fontStyle: 'italic', color: 'var(--muted-foreground, #999)' }
      }, '— compacted by context compression (original row kept in store) —'))
    }

    if (m.reasoning) {
      children.push(e(Foldable, { key: 'r', label: 'reasoning · ' + m.reasoning.length + ' chars' }, m.reasoning))
    }

    if (m.role === 'assistant' && Array.isArray(m.tool_calls)) {
      m.tool_calls.forEach(function (c, i) {
        var fn = (c && c.function) || {}
        children.push(e('div', {
          key: 'tc' + i,
          style: {
            marginTop: 6, padding: 8, borderRadius: 6,
            border: '1px solid var(--border, #ddd)', fontSize: '12px'
          }
        },
          e('div', { style: { fontFamily: 'monospace', color: 'var(--accent, #2563eb)' } }, '⚙ ' + (fn.name || '(unnamed)')),
          e('pre', {
            style: {
              marginTop: 4, fontSize: '11px', whiteSpace: 'pre-wrap',
              wordBreak: 'break-all', margin: '4px 0 0', color: 'var(--muted-foreground, #555)'
            }
          }, truncate(prettyArgs(fn.arguments), 4000))
        ))
      })
    }

    if (m.content != null && m.content !== '') {
      var long = String(m.content).length > 1500
      children.push(long
        ? e(Foldable, { key: 'x', label: 'content · ' + String(m.content).length + ' chars' }, m.content)
        : e('div', {
            key: 'x',
            style: { marginTop: 4, fontSize: '12.5px', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }
          }, m.content))
    }

    return e('div', {
      style: {
        padding: 10, borderRadius: 8, border: '1px solid var(--border, #ddd)',
        opacity: isCompacted ? 0.6 : 1
      }
    }, children)
  }

  // --------------------------------------------------------------- Main page
  function TracePage() {
    var sessionsState = useState({ list: [], error: null, loading: true })
    var sessions = sessionsState[0], setSessions = sessionsState[1]
    var selState = useState('')
    var sel = selState[0], setSel = selState[1]
    var qState = useState('')
    var q = qState[0], setQ = qState[1]
    var traceState = useState({ msgs: null, error: null, loading: false })
    var trace = traceState[0], setTrace = traceState[1]
    var orderState = useState('latest')
    var order = orderState[0], setOrder = orderState[1]

    function loadSessions(query) {
      setSessions(function (s) { return { list: s.list, error: null, loading: true } })
      fetchJSON(API + '/sessions?limit=100&q=' + encodeURIComponent(query || ''))
        .then(function (d) { setSessions({ list: d.sessions || [], error: null, loading: false }) })
        .catch(function (err) { setSessions({ list: [], error: String(err.message || err), loading: false }) })
    }

    useEffect(function () { loadSessions('') }, [])

    useEffect(function () {
      if (!sel) { setTrace({ msgs: null, error: null, loading: false }); return undefined }
      var alive = true
      setTrace({ msgs: null, error: null, loading: true })
      fetchJSON(API + '/sessions/' + encodeURIComponent(sel) + '/messages?limit=300&order=' + order)
        .then(function (d) { if (alive) setTrace({ msgs: d.messages || [], error: null, loading: false }) })
        .catch(function (err) { if (alive) setTrace({ msgs: null, error: String(err.message || err), loading: false }) })
      return function () { alive = false }
    }, [sel, order])

    function doExport() {
      fetchJSON(API + '/sessions/' + encodeURIComponent(sel) + '/export')
        .then(function (data) {
          var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
          var url = URL.createObjectURL(blob)
          var a = document.createElement('a')
          a.href = url
          a.download = 'trace_' + sel + '.json'
          document.body.appendChild(a)
          a.click()
          a.remove()
          URL.revokeObjectURL(url)
          alert('Exported ' + (data.meta && data.meta.message_count) + ' messages → trace_' + sel + '.json')
        })
        .catch(function (err) { alert('Export failed: ' + (err.message || err)) })
    }

    return e('div', { style: Object.assign({}, baseStyle, { display: 'flex', height: '100%', gap: 12 }) },
      // left: sessions
      e('div', { style: { width: 280, flexShrink: 0, display: 'flex', flexDirection: 'column', borderRight: '1px solid var(--border, #ddd)', paddingRight: 8 } },
        e('input', {
          value: q,
          placeholder: 'Search sessions…',
          onChange: function (ev) { setQ(ev.target.value); loadSessions(ev.target.value) },
          style: {
            width: '100%', boxSizing: 'border-box', padding: '6px 8px', marginBottom: 8,
            border: '1px solid var(--border, #ccc)', borderRadius: 6, fontFamily: 'inherit',
            fontSize: '12px', background: 'var(--card, #fff)', color: 'inherit'
          }
        }),
        e('div', { style: { overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: 2 } },
          sessions.loading ? e('div', { style: { padding: 12, color: 'var(--muted-foreground, #888)' } }, 'Loading…') : null,
          sessions.error ? e('div', { style: { padding: 12, color: '#dc2626', fontSize: '12px' } }, 'Backend unavailable: ' + sessions.error) : null,
          sessions.list.map(function (s) {
            return e('button', {
              key: s.id,
              type: 'button',
              onClick: function () { setSel(s.id) },
              style: {
                textAlign: 'left', padding: '6px 8px', borderRadius: 6, cursor: 'pointer',
                border: sel === s.id ? '1px solid var(--accent, #2563eb)' : '1px solid transparent',
                background: sel === s.id ? 'var(--accent-soft, rgba(37,99,235,0.08))' : 'none',
                fontFamily: 'inherit', fontSize: '12px', color: 'inherit'
              }
            },
              e('div', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, s.title),
              e('div', { style: { fontSize: '10px', color: 'var(--muted-foreground, #999)', display: 'flex', gap: 6 } },
                e('span', null, (s.message_count || 0) + ' msgs'),
                e('span', null, shortTime(s.last_activity_at || s.started_at)),
                s.model ? e('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis' } }, s.model) : null
              )
            )
          })
        )
      ),
      // right: trace
      e('div', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' } },
        sel ? e('div', { style: { display: 'flex', alignItems: 'center', gap: 8, paddingBottom: 8, borderBottom: '1px solid var(--border, #ddd)', marginBottom: 8 } },
          e('div', { style: { fontFamily: 'monospace', fontSize: '12px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, sel),
          e('div', { style: { marginLeft: 'auto', display: 'flex', gap: 6 } },
            e('button', {
              type: 'button', onClick: function () { setOrder('latest') },
              style: { fontSize: '11px', padding: '3px 8px', borderRadius: 5, cursor: 'pointer', border: '1px solid var(--border, #ccc)', background: order === 'latest' ? 'var(--accent-soft, #eef)' : 'none', color: 'inherit', fontFamily: 'inherit' }
            }, 'latest 300'),
            e('button', {
              type: 'button', onClick: function () { setOrder('oldest') },
              style: { fontSize: '11px', padding: '3px 8px', borderRadius: 5, cursor: 'pointer', border: '1px solid var(--border, #ccc)', background: order === 'oldest' ? 'var(--accent-soft, #eef)' : 'none', color: 'inherit', fontFamily: 'inherit' }
            }, 'from start'),
            e('button', {
              type: 'button', onClick: doExport,
              style: { fontSize: '11px', padding: '3px 10px', borderRadius: 5, cursor: 'pointer', border: '1px solid var(--accent, #2563eb)', background: 'var(--accent, #2563eb)', color: '#fff', fontFamily: 'inherit' }
            }, '⬇ Export messages[] JSON')
          )
        ) : null,
        e('div', { style: { overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: 8 } },
          !sel ? e('div', { style: { padding: 24, color: 'var(--muted-foreground, #888)' } }, 'Pick a session from the list to inspect its trace.') : null,
          trace.loading ? e('div', { style: { padding: 24, color: 'var(--muted-foreground, #888)' } }, 'Loading trace…') : null,
          trace.error ? e('div', { style: { padding: 24, color: '#dc2626' } }, 'Failed to load trace: ' + trace.error) : null,
          trace.msgs ? trace.msgs.map(function (m) { return e(MessageCard, { key: m.row_id, m: m }) }) : null
        )
      )
    )
  }

  window.__HERMES_PLUGINS__.register('agent-trace-viewer', TracePage)
})()
