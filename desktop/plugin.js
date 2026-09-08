import {
  atom,
  Badge,
  Button,
  Codicon,
  ErrorState,
  Input,
  Loader,
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogTitle,
  DialogDescription,
  STATUSBAR_AREAS,
  Tabs,
  TabsList,
  TabsTrigger,
  host,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'automation-lab-marketplace'
const GITHUB_DEVICE_URL = 'https://github.com/login/device'
const key = (...parts) => [ID, ...parts]
const currentScope = () => JSON.stringify([host.state.connectionId.get(), host.state.profile.get()])

// Release engineering replaces this only in a reviewed immutable launcher artifact.
// Never populated from storage, RPC, query strings or user-entered repository URLs.
const BOOTSTRAP_RELEASE = null

async function nativeCli(active, gateway, profile, argv, timeoutMs = 240_000) {
  if (!active() || !gateway || host.getGateway() !== gateway) throw new Error('Backend/profile changed; reopen the marketplace')
  if (typeof profile !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(profile) || ['all', 'current'].includes(profile.toLowerCase())) throw new Error('An explicit profile is required')
  const timeout = Math.min(600, Math.ceil(timeoutMs / 1000))
  const result = await gateway.request('cli.exec', { argv: ['-p', profile, ...argv], timeout }, timeout * 1000 + 15_000)
  if (!active() || host.getGateway() !== gateway) throw new Error('Backend/profile changed; discarded old response')
  if (result?.blocked || result?.code !== 0) throw new Error('Native command failed or was denied. Nothing will be retried automatically.')
  return result.output
}

async function scopedRest(ctx, active, scope, target, path, options = {}, gateway = host.getGateway()) {
  if (!active() || !gateway || host.getGateway() !== gateway) throw new Error('Backend/profile changed; reopen the marketplace')
  const profile = JSON.parse(scope)[1]
  if (path === '/state') {
    const inventory = await gateway.request('plugins.manage', { action: 'list', profile })
    if (!active() || host.getGateway() !== gateway) throw new Error('Backend/profile changed; discarded old response')
    if (!Array.isArray(inventory?.plugins)) throw new Error('Native plugin inventory unavailable')
    const installed = inventory.plugins.find(row => row.name === ID && row.source !== 'bundled')
    if (!installed || installed.status !== 'enabled') {
      const error = new Error(installed ? 'Marketplace Agent component is disabled; enable the reviewed installation in Agent Plugins.' : 'Marketplace Agent component is not installed on this agent.')
      error.setupRequired = !installed
      throw error
    }
  } else if (!target || target.profile !== profile) throw new Error('Installation destination is unconfirmed')
  const output = await nativeCli(active, gateway, profile, ['automation-lab', path.slice(1), '--profile-name', profile,
    '--request', JSON.stringify({ ...options.body, ...(target ? { expected_target: target.id } : {}) })], options.timeoutMs)
  const lines = output.split('\n').filter(line => line.startsWith('AUTOMATION_LAB_JSON:'))
  if (lines.length !== 1) throw new Error('Marketplace CLI is incompatible; install the reviewed Agent release')
  const envelope = JSON.parse(lines[0].slice('AUTOMATION_LAB_JSON:'.length))
  if (!envelope.ok) throw new Error(envelope.error || 'Marketplace operation failed')
  const result = envelope.result
  if (result?.target?.protocol !== 1 || !result?.target?.id || result.target.profile !== profile || (target && result.target.id !== target.id)) {
    throw new Error('Backend cannot confirm the selected installation destination; reopen the marketplace')
  }
  return result
}

async function setupAgent(ctx, active, scope, gateway) {
  const release = BOOTSTRAP_RELEASE
  if (!release || !/^[0-9a-f]{40}$/.test(release.revision)) throw new Error('Publication gate: no reviewed immutable release is configured')
  const profile = JSON.parse(scope)[1]
  await nativeCli(active, gateway, profile, ['plugins', 'install', release.source, '--ref', release.revision, '--no-enable'])
  // Stock exact-ref installer verifies the detached commit and writes pinned
  // provenance before returning. Never force past scanner/collision refusal.
  await nativeCli(active, gateway, profile, ['plugins', 'enable', ID])
  const state = await scopedRest(ctx, active, scope, null, '/state', {}, gateway)
  if (!state.backend?.enabled || !state.backend.pinned || state.backend.revision !== release.revision || state.backend.source !== release.source) {
    throw new Error('Setup provenance did not match the reviewed release; do not connect GitHub')
  }
  return state
}

function createPage(ctx, scopeState, refreshScope) {
  function MarketplacePage({ scope, initialTab }) {
    const [connection, profile] = JSON.parse(scope)
    const alive = useRef(true)
    useEffect(() => {
      alive.current = true
      return () => { alive.current = false }
    }, [])
    const active = () => alive.current && scopeState.get() === scope && currentScope() === JSON.stringify([connection, profile]) && host.getGateway() === gateway
    const target = useRef(null)
    const gateway = useRef(host.getGateway()).current
    const rest = (path, options) => scopedRest(ctx, active, scope, target.current, path, options, gateway)
    const queryClient = useQueryClient()
    const [search, setSearch] = useState('')
    const [flow, setFlow] = useState(null)
    const [copyStatus, setCopyStatus] = useState('idle')
    const copyPending = useRef(false)
    const liveFlow = useRef(null)
    liveFlow.current = flow
    const [tab, setTab] = useState(initialTab || null)
    const [marketplace, setMarketplace] = useState('all')
    const installPending = useRef(false)
    const [review, setReview] = useState(null)
    const [removal, setRemoval] = useState(null)
    const [restartNeeded, setRestartNeeded] = useState(false)

    const state = useQuery({
      queryKey: key('state', scope),
      queryFn: () => rest('/state'),
      retry: false
    })
    if (!target.current && !state.isError) target.current = state.data?.target
    const destinationName = useQuery({
      queryKey: key('connection-label', scope), retry: false,
      queryFn: async () => {
        if (connection === 'local') return 'Local'
        const rows = await host.connections()
        if (!active()) throw new Error('Connection changed')
        return rows.find(row => row.id === connection)?.label || 'Connection name unavailable'
      }
    })
    const destination = `${destinationName.data || 'Connection name unavailable'} → ${target.current?.profile || 'destination unavailable'}`
    const catalog = useQuery({
      queryKey: key('catalog', scope, target.current?.id),
      staleTime: 3_600_000,
      refetchOnWindowFocus: false,
      queryFn: () => rest('/catalog', { timeoutMs: 120_000 }),
      enabled: !state.isError && state.data?.connected === true && !!target.current,
      retry: false
    })
    const openGitHub = async () => {
      if (!active()) return
      // ponytail: the SDK uses native IPC; window.open is denied by Electron.
      if (!await ctx.os.openExternal(GITHUB_DEVICE_URL) && active()) {
        host.notify({ kind: 'error', message: 'Could not open your browser. Open https://github.com/login/device manually and enter the code shown here.' })
      }
    }
    const copyCode = async () => {
      if (!active() || !flow || liveFlow.current !== flow || copyPending.current) return
      copyPending.current = true
      setCopyStatus('pending')
      try {
        const copied = await ctx.os.writeClipboard(flow.user_code)
        if (active() && liveFlow.current === flow) setCopyStatus(copied === true ? 'copied' : 'failed')
      } catch {
        if (active() && liveFlow.current === flow) setCopyStatus('failed')
      } finally {
        copyPending.current = false
      }
    }
    const startAuth = useMutation({
      mutationFn: () => rest('/auth/start', { method: 'POST' }),
      onSuccess: next => {
        if (!active()) return
        setCopyStatus('idle')
        setFlow(next)
      },
      onError: error => active() && host.notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not start GitHub connection' })
    })
    const disconnect = useMutation({
      mutationFn: () => rest('/logout', { method: 'POST' }),
      onSuccess: async () => {
        if (!active()) return
        setFlow(null)
        await queryClient.invalidateQueries({ queryKey: key('state', scope) })
        queryClient.removeQueries({ queryKey: key('catalog', scope) })
      }
    })
    const install = useMutation({
      mutationFn: ({ name, marketplace, reviewed_revision }) =>
        rest('/install', {
          method: 'POST',
          body: { name, marketplace, reviewed_revision, enable: true },
          timeoutMs: 700_000
        }),
      onSuccess: result => {
        if (!active()) return
        if (result.review_required) { setReview(result); return }
        setReview(null)
        if (result.restart_required) setRestartNeeded(true)
        host.notify({ kind: 'success', message: `${result.name} ${result.version} installed` })
        void queryClient.invalidateQueries({ queryKey: key('catalog', scope) })
      },
      onError: error => active() && host.notify({ kind: 'error', message: error instanceof Error ? error.message : 'Install failed' }),
      onSettled: () => { installPending.current = false }
    })
    const installPackage = item => {
      if (!active() || installPending.current || remove.isPending || toggle.isPending) return
      // ponytail: one manual install at a time, matching the backend lock.
      installPending.current = true
      install.mutate(item)
    }

    const remove = useMutation({
      mutationFn: item => rest('/uninstall', { method: 'POST', body: { name: item.name, marketplace: item.marketplace, confirm_name: item.name } }),
      onSuccess: () => {
        if (!active()) return
        setRemoval(null)
        setRestartNeeded(true)
        void queryClient.invalidateQueries({ queryKey: key('catalog', scope) })
      },
      onError: error => active() && host.notify({ kind: 'error', message: error.message })
    })
    const toggle = useMutation({
      mutationFn: item => rest('/enabled', { method: 'POST', body: { name: item.name, enabled: !item.enabled } }),
      onSuccess: () => {
        if (!active()) return
        setRestartNeeded(true)
        void catalog.refetch()
      },
      onError: error => active() && host.notify({ kind: 'error', message: error.message })
    })
    useEffect(() => {
      if (!flow) return undefined
      let stopped = false
      let timer
      const poll = async () => {
        try {
          const result = await rest('/auth/poll', {
            method: 'POST',
            body: { flow_id: flow.flow_id }
          })
          if (stopped || !active()) return
          if (result.status === 'connected') {
            setFlow(null)
            await queryClient.invalidateQueries({ queryKey: key('state', scope) })
            if (active()) host.notify({ kind: 'success', message: `Connected to GitHub as ${result.username}` })
            return
          }
          timer = window.setTimeout(poll, Math.max(1, result.retry_after || flow.interval) * 1000)
        } catch (error) {
          if (!stopped && active()) {
            setFlow(null)
            host.notify({ kind: 'error', message: error instanceof Error ? error.message : 'GitHub connection failed' })
          }
        }
      }
      timer = window.setTimeout(poll, flow.interval * 1000)
      return () => {
        stopped = true
        window.clearTimeout(timer)
      }
    }, [connection, ctx, flow, profile, queryClient])

    const setupPending = useRef(false)
    const setup = useMutation({
      mutationFn: () => setupAgent(ctx, active, scope, gateway),
      onSuccess: result => {
        if (!active()) return
        target.current = result.target
        queryClient.setQueryData(key('state', scope), result)
      },
      onError: error => { if (active()) host.notify({ kind: 'error', message: error.message }) },
      onSettled: () => { setupPending.current = false }
    })
    const plugins = catalog.isError ? [] : catalog.data?.plugins || []
    useEffect(() => {
      if (catalog.data && tab === null) setTab(plugins.some(item => item.installed) ? 'installed' : 'browse')
    }, [catalog.data, tab])
    const selectedTab = tab || (plugins.some(item => item.installed) ? 'installed' : 'browse')
    const groups = useMemo(() => {
      const needle = search.trim().toLowerCase()
      const matches = (catalog.isError ? [] : catalog.data?.plugins || []).filter(item =>
        (marketplace === 'all' || item.marketplace === marketplace) &&
        `${item.name} ${item.display_name} ${item.description}`.toLowerCase().includes(needle))
      return {
        browse: matches.filter(item => !item.installed),
        installed: matches.filter(item => item.installed),
        updates: matches.filter(item => item.installed && item.update_available)
      }
    }, [catalog.data, catalog.isError, search, marketplace])
    const rows = groups[selectedTab]
    const marketplaces = [...new Map(plugins.map(item => [item.marketplace, item.marketplace_label])).entries()]

    if (state.isLoading) return jsx(Loader, { type: 'lemniscate-bloom' })
    if (state.isError) {
      const message = state.error instanceof Error ? state.error.message : 'Backend connection failed'
      const missing = state.error?.setupRequired === true
      return jsx(ErrorState, {
        title: missing ? `Set up Marketplace for ${destinationName.data || 'Connection name unavailable'} → ${profile}` : 'Installation destination unavailable',
        description: missing
          ? BOOTSTRAP_RELEASE
            ? `You are administering this agent. Install and enable ${BOOTSTRAP_RELEASE.source} at ${BOOTSTRAP_RELEASE.revision} in profile ${profile}. Profiles separate state, not OS permissions. No backend restart is needed.`
            : 'Publication gate: no reviewed immutable Agent release is configured. Setup is implemented but cannot install an unpublished release. Nothing has been installed or enabled.'
          : message,
        children: jsxs('div', { className: 'flex gap-2', children: [
          missing ? jsx(Button, { disabled: !BOOTSTRAP_RELEASE || setup.isPending, onClick: () => { if (active() && !setupPending.current) { setupPending.current = true; setup.mutate() } }, children: setup.isPending ? 'Setting up…' : 'Set up on this agent' }) : null,
          jsx(Button, { disabled: setup.isPending, onClick: () => { if (active()) refreshScope() }, children: 'Retry destination check' })
        ] })
      })
    }

    return jsxs('div', {
      className: 'flex h-full flex-col overflow-hidden',
      children: [
        jsxs('header', {
          className: 'flex shrink-0 items-center justify-between gap-4 border-b border-(--ui-stroke-secondary) px-6 py-4',
          children: [
            jsxs('div', {
              children: [
                jsx('h1', { className: 'text-lg font-semibold', children: 'Automation Lab Marketplace' }),
                jsx('p', {
                  className: 'mt-1 text-sm text-(--ui-text-tertiary)',
                  children: `Installing to: ${destination}`
                })
              ]
            }),
            state.data?.connected
              ? jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx(Badge, { children: `GitHub: ${state.data.username || 'connected'}` }),
                    jsx(Button, {
                      variant: 'ghost',
                      disabled: disconnect.isPending,
                      onClick: () => disconnect.mutate(),
                      children: 'Disconnect'
                    })
                  ]
                })
              : null
          ]
        }),
        restartNeeded
          ? jsxs('div', {
              className: 'flex shrink-0 items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-6 py-3',
              children: [
                jsx('span', { className: 'text-sm', children: 'Restart once when you have finished changing plugins.' }),
                jsx(Button, { onClick: () => { if (active()) void host.restartGateway() }, children: 'Restart Hermes' })
              ]
            })
          : null,
        !state.data?.configured
          ? jsx(ErrorState, {
              title: 'Marketplace setup incomplete',
              description: 'The Automation Lab GitHub App has not been configured yet.'
            })
          : !state.data?.connected
            ? jsx('div', {
                className: 'grid flex-1 place-items-center p-6',
                children: flow
                  ? jsxs('div', {
                      className: 'max-w-md text-center',
                      children: [
                        jsx(Codicon, { name: 'github', className: 'mb-3 text-3xl' }),
                        jsx('h2', { className: 'text-lg font-semibold', children: 'Approve GitHub access' }),
                        jsxs('ol', { className: 'mt-4 space-y-4 text-left', children: [
                          jsxs('li', { children: [
                            jsx('h3', { className: 'font-medium', children: '1. Copy code' }),
                            jsx('div', { className: 'my-2 select-text rounded-md border border-(--ui-stroke-secondary) p-4 font-mono text-xl tracking-widest', children: flow.user_code }),
                            jsx(Button, { disabled: copyStatus === 'pending', onClick: () => void copyCode(),
                              children: copyStatus === 'pending' ? 'Copying…' : copyStatus === 'copied' ? 'Copied' : 'Copy code' }),
                            jsx('p', { role: 'status', className: 'mt-2 text-sm', children:
                              copyStatus === 'failed' ? 'Could not copy. Select the code above and copy it manually, or try again.' :
                              copyStatus === 'copied' ? 'Code copied to clipboard.' : '' })
                          ] }),
                          jsxs('li', { children: [
                            jsx('h3', { className: 'font-medium', children: '2. Open GitHub and paste code' }),
                            jsx(Button, { asChild: true, variant: 'link', children: jsx('a', {
                              href: GITHUB_DEVICE_URL,
                              onClick: event => { event.preventDefault(); void openGitHub() },
                              children: 'Open GitHub'
                            }) }),
                            jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: GITHUB_DEVICE_URL })
                          ] }),
                          jsxs('li', { children: [
                            jsx('h3', { className: 'font-medium', children: '3. Return here' }),
                            jsx('p', { role: 'status', className: 'mt-2 text-sm text-(--ui-text-tertiary)', children: 'Waiting for GitHub approval. Your connection will finish automatically here.' })
                          ] })
                        ] })
                      ]
                    })
                  : jsxs('div', {
                      className: 'grid min-h-48 place-items-center gap-4 text-center',
                      children: [
                        jsxs('div', {
                          children: [
                            jsx('h2', { className: 'text-lg font-semibold', children: 'Connect your Lab access' }),
                            jsx('p', {
                              className: 'mt-2 text-sm text-(--ui-text-tertiary)',
                              children: 'Connect GitHub once to confirm your Lab membership. No terminal, PAT or SSH key is needed.'
                            }),
                            jsx('p', {
                              className: 'mt-2 max-w-lg text-xs text-(--ui-text-quaternary)',
                              children: 'GitHub access and installed packages are saved to the confirmed destination above. This is machine-level administration; profiles are not security sandboxes.'
                            })
                          ]
                        }),
                        jsx(Button, {
                          disabled: startAuth.isPending,
                          onClick: () => startAuth.mutate(),
                          children: startAuth.isPending ? 'Starting…' : 'Connect GitHub'
                        })
                      ]
                    })
              })
            : jsxs('main', {
                className: 'flex min-h-0 flex-1 flex-col gap-4 p-6',
                children: [
                  removal ? jsxs('section', { role: 'region', 'aria-label': 'Confirm uninstall', children: [
                    jsx('h2', { children: `Uninstall ${removal.name} from ${destination}?` }),
                    jsx('p', { children: 'Disable keeps the package installed. Uninstall removes its entire package folder and install record. Separate user skills, memories and plugin data are kept. Files edited inside the package folder will be removed. Restart afterward to unload running code.' }),
                    jsx(Button, { disabled: remove.isPending || install.isPending || toggle.isPending, onClick: () => { if (active()) remove.mutate(removal) }, children: `Confirm uninstall ${removal.name} from ${destination}` }),
                    jsx(Button, { variant: 'ghost', disabled: remove.isPending, onClick: () => setRemoval(null), children: 'Cancel uninstall' })
                  ] }) : null,
                  review ? jsxs('section', { role: 'region', 'aria-label': 'Plugin security review', children: [
                    jsx('h2', { children: `Review ${review.name} before installing` }),
                    jsx('pre', { className: 'max-h-64 overflow-auto whitespace-pre-wrap text-xs', children: review.report }),
                    jsx('p', { children: 'Only continue if you trust this exact revision. Dangerous findings cannot be overridden.' }),
                    jsx(Button, { disabled: install.isPending, onClick: () => installPackage({
                      name: review.name, marketplace: review.marketplace, reviewed_revision: review.revision
                    }), children: install.isPending ? 'Installing…' : 'I reviewed the findings — install this revision' }),
                    jsx(Button, { variant: 'ghost', onClick: () => setReview(null), children: 'Cancel' })
                  ] }) : null,
                  jsxs(Tabs, { value: selectedTab, onValueChange: setTab, children: [
                    jsx(TabsList, { 'aria-label': 'Plugin views', children: Object.entries({ browse: 'Browse', installed: 'Installed', updates: 'Updates' }).map(([value, label]) =>
                      jsx(TabsTrigger, { value, id: `marketplace-tab-${value}`, 'aria-controls': 'marketplace-results', children: `${label} (${catalog.isError || !catalog.data ? '?' : groups[value].length})` }, value)) })
                  ] }),
                  jsxs('label', { className: 'flex items-center gap-2 text-sm', children: [
                    'Marketplace',
                    jsx('select', { 'aria-label': 'Marketplace', value: marketplace, onChange: event => setMarketplace(event.target.value),
                      children: [jsx('option', { value: 'all', children: 'All marketplaces' }),
                        ...marketplaces.map(([value, label]) => jsx('option', { value, children: label }, value))] })
                  ] }),
                  install.isPending ? jsx('p', { role: 'status', children: `Installing ${install.variables?.name} from ${install.variables?.marketplace}… This may take a little while.` }) : null,
                  jsx(Input, {
                    value: search,
                    onChange: event => setSearch(event.target.value),
                    placeholder: 'Search plugins…',
                    'aria-label': 'Search Automation Lab plugins'
                  }),
                  catalog.isLoading
                    ? jsx(Loader, { type: 'lemniscate-bloom' })
                    : catalog.isError
                      ? jsx(ErrorState, {
                          title: 'Could not load the marketplace',
                          description: catalog.error instanceof Error ? catalog.error.message : 'Try again.',
                          children: jsxs('div', {
                            className: 'flex gap-2',
                            children: [
                              jsx(Button, { onClick: () => void catalog.refetch(), children: 'Try again' }),
                              jsx(Button, { variant: 'ghost', onClick: () => disconnect.mutate(), children: 'Reconnect GitHub' })
                            ]
                          })
                        })
                      : jsxs('div', {
                          role: 'tabpanel', id: 'marketplace-results', 'aria-labelledby': `marketplace-tab-${selectedTab}`, tabIndex: 0,
                          className: 'grid min-h-0 flex-1 auto-rows-max grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3 overflow-y-auto',
                          children: [!rows.length ? jsx('p', { role: 'status', children:
                            search.trim() || marketplace !== 'all'
                              ? 'No plugins match these filters in this tab. Try another tab or clear your search and marketplace filter.'
                              : !plugins.length ? 'No plugins are available to this GitHub account. Only marketplaces you have access to are shown.'
                                : { browse: 'You’ve installed all available plugins. Manage them in Installed.', installed: 'No plugins installed yet. Find your first plugin in Browse.', updates: 'All up to date. No plugin updates are available.' }[selectedTab]
                          }) : null, ...rows.map(item =>
                            jsxs('article', {
                              className: 'flex flex-col rounded-lg border border-(--ui-stroke-secondary) p-4',
                              children: [
                                jsxs('div', {
                                  className: 'flex items-start justify-between gap-3',
                                  children: [
                                    jsx('h2', { className: 'font-medium', children: item.display_name }),
                                    jsx(Badge, { children: item.version || 'latest' })
                                  ]
                                }),
                                jsx('p', {
                                  className: 'mt-2 line-clamp-3 text-sm text-(--ui-text-tertiary)',
                                  children: item.description
                                }),
                                jsx('p', {
                                  className: 'mt-3 text-xs text-(--ui-text-quaternary)',
                                  children: `${item.marketplace_label} · ${item.skills} skills · ${item.connectors} connectors`
                                }),
                                item.installed ? jsx('p', { className: 'mt-2 text-sm', children: `Installed ${item.installed_version || 'version unknown'} · ${item.enabled ? 'Enabled' : 'Disabled'}` }) : null,
                                item.installed && !item.source_conflict ? jsx(Button, {
                                  variant: 'ghost', disabled: toggle.isPending || install.isPending || remove.isPending,
                                  onClick: () => toggle.mutate(item), children: item.enabled ? 'Disable' : 'Enable'
                                }) : null,
                                item.installed && !item.source_conflict && item.name !== ID ? jsx(Button, {
                                  variant: 'ghost', disabled: toggle.isPending || install.isPending || remove.isPending,
                                  onClick: () => { if (active()) setRemoval(item) }, children: 'Uninstall'
                                }) : null,
                                jsx('div', {
                                  className: 'mt-auto pt-4',
                                  children: jsx(Button, {
                                    disabled: install.isPending || remove.isPending || toggle.isPending || (item.installed && !item.update_available),
                                    onClick: () => installPackage({ name: item.name, marketplace: item.marketplace }),
                                    children: install.isPending && install.variables?.name === item.name && install.variables?.marketplace === item.marketplace
                                      ? 'Installing…'
                                      : item.source_conflict
                                        ? 'Different source'
                                        : item.downgrade_blocked
                                          ? 'Installed newer'
                                          : item.update_available
                                            ? 'Update'
                                            : item.installed
                                              ? 'Installed'
                                              : 'Install'
                                  })
                                })
                              ]
                            },
                            `${item.marketplace}:${item.name}`)
                          )]
                        })
                ]
              })
      ]
    })
  }
  return function ScopedMarketplace({ initialTab }) {
    const scope = useValue(scopeState)
    return jsx(MarketplacePage, { scope, initialTab }, scope)
  }
}

export default {
  id: ID,
  name: 'Automation Lab Marketplace',
  defaultEnabled: true,
  register(ctx) {
    let generation = Date.now()
    const scopeState = atom(JSON.stringify([...JSON.parse(currentScope()), generation]))
    const changed = () => scopeState.set(JSON.stringify([...JSON.parse(currentScope()), ++generation]))
    ctx.onDispose(host.state.connectionId.listen(changed))
    ctx.onDispose(host.state.profile.listen(changed))
    const MarketplacePage = createPage(ctx, scopeState, changed)
    function Launcher({ scope }) {
      const [connection, profile] = JSON.parse(scope)
      const [initialTab, setInitialTab] = useState(null)
      const opener = useRef(null)
      const mainTrigger = useRef(null)
      const alive = useRef(true)
      useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
      const active = () => alive.current && scopeState.get() === scope && currentScope() === JSON.stringify([connection, profile]) && host.getGateway() === gateway
      const target = useRef(null)
      const gateway = useRef(host.getGateway()).current
    const rest = (path, options) => scopedRest(ctx, active, scope, target.current, path, options, gateway)
      // ponytail: share cache within a scope generation; switches always start fresh.
      const state = useQuery({ queryKey: key('state', scope), queryFn: () => rest('/state'),
        retry: false, staleTime: 3_600_000, refetchInterval: 3_600_000, refetchIntervalInBackground: true, refetchOnWindowFocus: false })
      if (!target.current && !state.isError) target.current = state.data?.target
      const catalog = useQuery({ queryKey: key('catalog', scope, target.current?.id),
        queryFn: () => rest('/catalog', { timeoutMs: 120_000 }), enabled: !state.isError && state.data?.connected === true && !!target.current,
        retry: false, staleTime: 3_600_000, refetchInterval: 3_600_000, refetchIntervalInBackground: true, refetchOnWindowFocus: false })
      const unknown = state.isError || !state.data?.target || !state.data?.connected || catalog.isError || !catalog.data
      const count = !unknown ? catalog.data.plugins.filter(item => item.installed && item.update_available).length : null
      return jsxs(Dialog, {
        children: [
          jsx(DialogTrigger, { asChild: true,
            children: jsx(Button, { ref: mainTrigger, variant: 'ghost', size: 'sm', onClick: event => { opener.current = event.currentTarget; setInitialTab(null) }, children: 'Automation Lab' }) }),
          count > 0 ? jsx(DialogTrigger, { asChild: true,
            children: jsx(Button, { variant: 'ghost', size: 'sm', style: { color: 'var(--ui-accent)' },
              'aria-label': `${count} updates available`, onClick: event => { opener.current = event.currentTarget; setInitialTab('updates') }, children: `+${count}` }) }) : null,
          unknown ? jsx('span', { role: 'status', 'aria-label': 'Update check unavailable', title: 'Updates unknown: checking, disconnected or unavailable. Open Automation Lab for details.', children: '?' }) : null,
          jsxs(DialogContent, {
            onCloseAutoFocus: event => { event.preventDefault(); (opener.current?.isConnected ? opener.current : mainTrigger.current)?.focus() },
            style: { width: '92vw', maxWidth: '1100px' },
            bodyClassName: 'flex min-h-0 flex-col',
            children: [
              jsx(DialogTitle, { children: 'Automation Lab Marketplace' }),
              jsx(DialogDescription, { children: 'Browse and manage your marketplace plugins.' }),
              jsx('div', { style: { height: '65vh', minHeight: 0 }, children: jsx(MarketplacePage, { initialTab }) })
            ]
          })
        ]
      })
    }
    function ScopedLauncher() {
      const scope = useValue(scopeState)
      return jsx(Launcher, { scope }, scope)
    }
    // ponytail: native Dialog owns dismissal and focus; no route or extra pane.
    ctx.register({ id: 'launcher', area: STATUSBAR_AREAS.left, render: () => jsx(ScopedLauncher, {}) })
  }
}
