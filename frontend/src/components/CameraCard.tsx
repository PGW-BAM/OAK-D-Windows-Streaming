import { useRef, useState, useEffect } from 'react'
import { DetectionOverlay } from './DetectionOverlay'
import { ImuOverlay } from './ImuOverlay'
import { enableCamera, disableCamera, stopRecording, useDetections, useImuData, useStreamUrl } from '../hooks/useCamera'
import type { CameraStatus } from '../types'

interface Props {
  camera: CameraStatus
  onSettings: (camera: CameraStatus) => void
  onRefresh: () => void
  maximized?: boolean
  onMaximize?: () => void
  onRestore?: () => void
}

export function CameraCard({ camera, onSettings, onRefresh, maximized, onMaximize, onRestore }: Props) {
  const streamUrl = useStreamUrl(camera.id)
  const detection = useDetections(camera.id)
  const imu = useImuData(camera.id)
  const imgRef = useRef<HTMLImageElement>(null)
  const [imgSize, setImgSize] = useState({ w: 0, h: 0 })
  const [imgError, setImgError] = useState(false)
  const [reconnectKey, setReconnectKey] = useState(0)
  const [toggling, setToggling] = useState(false)
  const [stopping, setStopping] = useState(false)

  // Auto-reconnect: when the MJPEG stream breaks (e.g. because the backend was
  // briefly blocked restarting a pipeline), wait 3 s then force a new <img>
  // mount so the browser opens a fresh HTTP connection.
  useEffect(() => {
    if (!imgError) return
    const t = setTimeout(() => {
      setImgError(false)
      setReconnectKey(k => k + 1)
    }, 3000)
    return () => clearTimeout(t)
  }, [imgError])

  const onLoad = () => {
    if (imgRef.current) {
      setImgSize({ w: imgRef.current.offsetWidth, h: imgRef.current.offsetHeight })
      setImgError(false)
    }
  }

  const handleStopRecording = async () => {
    setStopping(true)
    try {
      await stopRecording(camera.id)
      onRefresh()
    } finally {
      setStopping(false)
    }
  }

  const toggleEnabled = async () => {
    setToggling(true)
    try {
      if (camera.enabled) {
        await disableCamera(camera.id)
      } else {
        await enableCamera(camera.id)
      }
      onRefresh()
    } catch { /* ignore */ }
    finally { setToggling(false) }
  }

  const statusColor = !camera.enabled ? '#888' : camera.connected ? '#44dd66' : '#dd4444'

  return (
    <div
      style={{
        position: 'relative',
        width: '100%',
        height: '100%',
        background: '#0d0d1a',
        borderRadius: 6,
        overflow: 'hidden',
        border: '1px solid #333',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Stream or placeholder */}
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        {camera.enabled && camera.connected && !imgError ? (
          <>
            <img
              key={reconnectKey}
              ref={imgRef}
              src={streamUrl}
              onLoad={onLoad}
              onError={() => setImgError(true)}
              style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
              alt={camera.name}
            />
            {detection && imgSize.w > 0 && (
              <DetectionOverlay
                detection={detection}
                width={imgSize.w}
                height={imgSize.h}
              />
            )}
            {imu && <ImuOverlay imu={imu} />}
            {/* Recording overlay — visible on the stream so it can't be missed */}
            {camera.recording && (
              <div style={{
                position: 'absolute',
                top: 0,
                left: 0,
                right: 0,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '6px 10px',
                background: 'rgba(120,0,0,0.75)',
                backdropFilter: 'blur(4px)',
              }}>
                <span style={{
                  color: '#ff4444',
                  fontSize: 12,
                  fontFamily: 'monospace',
                  fontWeight: 700,
                  animation: 'recBlink 1.2s step-start infinite',
                }}>
                  {camera.recording_mode === 'video' ? '⏺ REC' : '📷 INT'}
                </span>
                <button
                  onClick={handleStopRecording}
                  disabled={stopping}
                  style={{
                    marginLeft: 'auto',
                    padding: '3px 12px',
                    background: stopping ? '#555' : '#cc2222',
                    border: 'none',
                    borderRadius: 4,
                    color: '#fff',
                    cursor: stopping ? 'default' : 'pointer',
                    fontSize: 12,
                    fontFamily: 'monospace',
                    fontWeight: 700,
                  }}
                >
                  {stopping ? 'Stopping…' : '■ Stop'}
                </button>
              </div>
            )}
          </>
        ) : (
          <div
            style={{
              width: '100%',
              height: '100%',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#555',
              fontSize: 14,
              gap: 6,
            }}
          >
            {camera.recording && (
              <button
                onClick={handleStopRecording}
                disabled={stopping}
                style={{
                  marginBottom: 6,
                  padding: '5px 16px',
                  background: stopping ? '#555' : '#cc2222',
                  border: 'none',
                  borderRadius: 4,
                  color: '#fff',
                  cursor: stopping ? 'default' : 'pointer',
                  fontSize: 13,
                  fontFamily: 'monospace',
                  fontWeight: 700,
                }}
              >
                {stopping ? 'Stopping…' : `■ Stop ${camera.recording_mode === 'video' ? 'video' : 'interval'}`}
              </button>
            )}
            {!camera.enabled ? (
              <>
                <span style={{ fontSize: 24, opacity: 0.4 }}>⏻</span>
                <span>Stream paused — bandwidth freed</span>
                <button
                  onClick={toggleEnabled}
                  disabled={toggling}
                  style={{
                    marginTop: 4,
                    padding: '4px 14px',
                    background: '#224422',
                    border: '1px solid #446644',
                    borderRadius: 4,
                    color: '#88cc88',
                    cursor: 'pointer',
                    fontSize: 12,
                    fontFamily: 'monospace',
                  }}
                >
                  Enable
                </button>
              </>
            ) : camera.connected ? (
              'Stream error — reconnecting…'
            ) : (
              'Camera disconnected'
            )}
          </div>
        )}
      </div>

      {/* Status bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '7px 12px',
          background: 'rgba(0,0,0,0.75)',
          fontSize: 15,
          color: '#ccc',
          fontFamily: 'monospace',
        }}
      >
        <span style={{ color: statusColor, fontSize: 11 }}>●</span>
        <span style={{ fontWeight: 700, color: '#eee' }}>{camera.name}</span>
        {camera.ip && <span style={{ color: '#888' }}>{camera.ip}</span>}
        <span>{camera.stream_fps} fps</span>
        {camera.resolution && <span style={{ color: '#888' }}>{camera.resolution}</span>}
        {camera.latency_ms > 0 && <span>{camera.latency_ms} ms</span>}
        {camera.recording && (
          <span style={{ color: '#ff4444' }}>
            {camera.recording_mode === 'video' ? '⏺ REC' : '📷 INT'}
          </span>
        )}
        {camera.stereo_mode !== 'main_only' && (
          <span style={{ color: '#88aaff' }}>
            {camera.stereo_mode === 'both' ? 'RGB+S' : 'Stereo'}
          </span>
        )}
        {camera.inference_mode !== 'none' && (
          <span style={{ color: '#88ff88' }}>
            {camera.inference_mode === 'on_camera' ? 'AI cam' : 'AI host'}
          </span>
        )}
        {camera.flip_180 && (
          <span style={{ color: '#ffaa44' }} title="Stream rotated 180°">↻180°</span>
        )}

        {/* Controls (right-aligned) */}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center' }}>
          <button
            onClick={toggleEnabled}
            disabled={toggling}
            title={camera.enabled ? 'Disable stream (free bandwidth)' : 'Enable stream'}
            style={{
              ...btnStyle,
              color: camera.enabled ? '#44dd66' : '#666',
              fontSize: 20,
            }}
          >
            ⏻
          </button>
          <button
            onClick={() => onSettings(camera)}
            title="Camera settings"
            style={btnStyle}
          >
            ⚙
          </button>
          {maximized ? (
            <button onClick={onRestore} title="Restore" style={btnStyle}>⊡</button>
          ) : (
            <button onClick={onMaximize} title="Maximize" style={btnStyle}>⊞</button>
          )}
        </span>
      </div>
    </div>
  )
}

const btnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: '#aaa',
  cursor: 'pointer',
  fontSize: 20,
  padding: '2px 6px',
  lineHeight: 1,
}
