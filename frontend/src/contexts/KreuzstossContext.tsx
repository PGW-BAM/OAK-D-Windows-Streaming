import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { KreuzstossMode } from '../hooks/useCamera'

interface KreuzstossCtx {
  running: boolean
  setRunning: (v: boolean) => void
  panelOpen: boolean
  panelMode: KreuzstossMode
  openPanel: (mode: KreuzstossMode) => void
  closePanel: () => void
  // Backwards-compatible alias kept so existing call sites keep working.
  setPanelOpen: (v: boolean) => void
}

const Ctx = createContext<KreuzstossCtx>({
  running: false,
  setRunning: () => {},
  panelOpen: false,
  panelMode: 'full',
  openPanel: () => {},
  closePanel: () => {},
  setPanelOpen: () => {},
})

export function KreuzstossProvider({ children }: { children: ReactNode }) {
  const [running, setRunningState] = useState(false)
  const [panelOpen, setPanelOpenState] = useState(false)
  const [panelMode, setPanelModeState] = useState<KreuzstossMode>('full')

  const setRunning = useCallback((v: boolean) => setRunningState(v), [])
  const openPanel = useCallback((mode: KreuzstossMode) => {
    setPanelModeState(mode)
    setPanelOpenState(true)
  }, [])
  const closePanel = useCallback(() => setPanelOpenState(false), [])
  const setPanelOpen = useCallback((v: boolean) => setPanelOpenState(v), [])

  const value = useMemo<KreuzstossCtx>(
    () => ({
      running, setRunning,
      panelOpen, panelMode,
      openPanel, closePanel, setPanelOpen,
    }),
    [running, panelOpen, panelMode, setRunning, openPanel, closePanel, setPanelOpen],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useKreuzstoss() {
  return useContext(Ctx)
}
