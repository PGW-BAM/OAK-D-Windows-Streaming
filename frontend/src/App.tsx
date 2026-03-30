import { useState } from 'react'
import { CameraGrid } from './components/CameraGrid'
import { useCameraList } from './hooks/useCamera'
import './App.css'

export default function App() {
  const { cameras, loading, discover, refresh } = useCameraList(3000)
  const [discovering, setDiscovering] = useState(false)

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
