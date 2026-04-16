import { useEffect, useRef, useState } from 'react'
import { CameraGrid } from './components/CameraGrid'
import {
  applySessionToCameras,
  getLastSession,
  saveSession,
  useCameraList,
  type SessionSnapshot,
} from './hooks/useCamera'
import './App.css'

export default function App() {
  const { cameras, loading, discover, refresh } = useCameraList(3000)
  const [discovering, setDiscovering] = useState(false)
  const [pendingSession, setPendingSession] = useState<SessionSnapshot | null>(null)
  const [sessionPrompted, setSessionPrompted] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const saveTimer = useRef<number | null>(null)

  // On first load with cameras present, check for a previous session
  useEffect(() => {
    if (sessionPrompted || loading || cameras.length === 0) return
    setSessionPrompted(true)
    getLastSession().then((snap) => {
      if (snap && Array.isArray(snap.cameras) && snap.cameras.length > 0) {
        setPendingSession(snap)
      }
    })
  }, [sessionPrompted, loading, cameras.length])

  // Debounced auto-save: snapshot current camera status after changes
  useEffect(() => {
    if (cameras.length === 0 || pendingSession) return
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => {
      const snap: SessionSnapshot = {
        saved_at: new Date().toISOString(),
        cameras: cameras.map((c) => ({
          id: c.id,
          stream_fps: c.stream_fps,
          mjpeg_quality: c.mjpeg_quality,
          resolution: c.resolution,
          stereo_mode: c.stereo_mode,
          flip_180: c.flip_180,
          inference_mode: c.inference_mode,
        })),
      }
      saveSession(snap)
    }, 2000)
    return () => {
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current)
    }
  }, [cameras, pendingSession])

  async function handleRestore() {
    if (!pendingSession) return
    setRestoring(true)
    try {
      await applySessionToCameras(pendingSession)
      await refresh()
    } finally {
      setRestoring(false)
      setPendingSession(null)
    }
  }

  function handleDismiss() {
    setPendingSession(null)
  }

  async function handleDiscover() {
    setDiscovering(true)
    await discover()
    setDiscovering(false)
  }

  return (
    <div style={{ minHeight: '100vh', background: '#0a0a14', color: '#eee' }}>
      {/* Header */}
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          padding: '10px 16px',
          background: '#111',
          borderBottom: '1px solid #333',
          fontFamily: 'monospace',
        }}
      >
        <span style={{ fontWeight: 700, fontSize: 16, letterSpacing: 1 }}>
          OAK-D Dashboard
        </span>
        <span style={{ color: '#666', fontSize: 12 }}>
          {cameras.filter((c) => c.connected).length}/{cameras.length} connected
        </span>
        <button
          onClick={handleDiscover}
          disabled={discovering}
          style={{
            marginLeft: 'auto',
            padding: '6px 14px',
            background: discovering ? '#333' : '#334',
            border: '1px solid #555',
            borderRadius: 4,
            color: '#cdf',
            cursor: discovering ? 'default' : 'pointer',
            fontSize: 12,
            fontFamily: 'monospace',
          }}
        >
          {discovering ? 'Scanning\u2026' : '+ Discover cameras'}
        </button>
      </header>

      {/* Restore-last-session modal */}
      {pendingSession && (
        <div
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
          }}
        >
          <div
            style={{
              background: '#14141d', border: '1px solid #334',
              borderRadius: 6, padding: 24, maxWidth: 440, fontFamily: 'monospace',
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 10 }}>
              Restore last session?
            </div>
            <div style={{ fontSize: 12, color: '#aaa', marginBottom: 16, lineHeight: 1.5 }}>
              Found saved settings for {pendingSession.cameras.length} camera(s) from{' '}
              {new Date(pendingSession.saved_at).toLocaleString()}.
              Apply them now?
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                onClick={handleDismiss}
                disabled={restoring}
                style={{
                  padding: '6px 14px', background: '#222', border: '1px solid #555',
                  borderRadius: 4, color: '#bbb', cursor: 'pointer', fontSize: 12,
                }}
              >
                Start fresh
              </button>
              <button
                onClick={handleRestore}
                disabled={restoring}
                style={{
                  padding: '6px 14px', background: '#2a4', border: '1px solid #3c6',
                  borderRadius: 4, color: '#fff', cursor: restoring ? 'default' : 'pointer',
                  fontSize: 12, fontWeight: 600,
                }}
              >
                {restoring ? 'Restoring\u2026' : 'Restore settings'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Main content */}
      <main>
        {loading ? (
          <div style={{ padding: 40, textAlign: 'center', color: '#555' }}>
            Connecting to backend\u2026
          </div>
        ) : cameras.length === 0 ? (
          <div style={{ padding: 60, textAlign: 'center', color: '#555' }}>
            <div style={{ fontSize: 32, marginBottom: 12 }}>📷</div>
            <div style={{ fontSize: 14 }}>No cameras found.</div>
            <div style={{ fontSize: 12, marginTop: 8, color: '#444' }}>
              Make sure OAK devices are connected and click &quot;Discover cameras&quot;.
            </div>
          </div>
        ) : (
          <CameraGrid cameras={cameras} onRefresh={refresh} />
        )}
      </main>
    </div>
  )
}
