import { useState } from 'react'
import {
  captureAngleTarget,
  deleteAngleTarget,
  useAngleTargets,
} from '../hooks/useCamera'
import type { IMUData } from '../types'

interface Props {
  cameraId: string          // OAK-D device mxid (for IMU source)
  camId: string             // logical cam_id ("cam1"/"cam2") for MQTT drive key
  imu: IMUData | null
  onFlash?: (msg: string) => void
}

const inputStyle: React.CSSProperties = {
  width: '100%', padding: '6px', background: '#111', color: '#eee',
  border: '1px solid #333', borderRadius: 4, fontFamily: 'monospace',
  fontSize: 11, boxSizing: 'border-box',
}

const btnPrimary: React.CSSProperties = {
  width: '100%', padding: '8px', border: 'none', borderRadius: 4,
  color: '#fff', cursor: 'pointer', fontSize: 12, fontFamily: 'monospace',
}

export function AngleTargetsPanel({ cameraId, camId, imu, onFlash }: Props) {
  const { targets, refresh } = useAngleTargets(camId)
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [activeAngle, setActiveAngle] = useState<'roll' | 'pitch'>('roll')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)

  const flash = (m: string) => onFlash?.(m)

  const handleCapture = async () => {
    if (!name.trim()) {
      flash('Checkpoint name required')
      return
    }
    setBusy(true)
    try {
      await captureAngleTarget({
        camera_id: cameraId,
        cam_id: camId,
        checkpoint_name: name.trim(),
        active_angle: activeAngle,
        label: label.trim(),
      })
      flash(`Taught ${name.trim()} (${activeAngle})`)
      setName('')
      setLabel('')
      await refresh()
    } catch (err) {
      flash(`Teach failed: ${(err as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (checkpointName: string) => {
    if (!confirm(`Delete angle target "${checkpointName}"?`)) return
    setBusy(true)
    try {
      await deleteAngleTarget(camId, checkpointName)
      await refresh()
    } catch {
      flash('Delete failed')
    } finally {
      setBusy(false)
    }
  }

  const count = targets.length

  return (
    <section style={{
      marginBottom: 16, background: '#15152a', padding: '8px 10px',
      borderRadius: 6, border: '1px solid #333',
    }}>
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          cursor: 'pointer', marginBottom: open ? 10 : 0,
        }}
      >
        <h4 style={{ margin: 0, color: '#fbb', fontSize: 13 }}>
          Radial angle teach ({camId})
        </h4>
        <span style={{ color: '#aaa', fontSize: 11 }}>
          {count} target{count === 1 ? '' : 's'} {open ? '▾' : '▸'}
        </span>
      </div>

      {open && (
        <>
          <div style={{
            fontSize: 11, color: imu ? '#8fc' : '#666', background: '#0d0d1a',
            padding: '6px 8px', borderRadius: 4, marginBottom: 8,
            fontFamily: 'monospace',
          }}>
            IMU:&nbsp;
            {imu
              ? <>Roll {imu.roll_deg.toFixed(2)}°&nbsp; Pitch {imu.pitch_deg.toFixed(2)}°</>
              : <>no data</>}
          </div>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, marginBottom: 6 }}>
            <span>Checkpoint name</span>
            <input
              type="text"
              placeholder="e.g. scan_top"
              value={name}
              onChange={(e) => setName(e.target.value)}
              style={inputStyle}
            />
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, marginBottom: 6 }}>
            <span>Active angle (IMU reference axis)</span>
            <select
              value={activeAngle}
              onChange={(e) => setActiveAngle(e.target.value as 'roll' | 'pitch')}
              style={{
                ...inputStyle,
                padding: '6px', fontFamily: 'inherit', fontSize: 12,
              }}
            >
              <option value="roll">roll</option>
              <option value="pitch">pitch</option>
            </select>
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, marginBottom: 6 }}>
            <span>Label (optional)</span>
            <input
              type="text"
              placeholder="notes"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              style={inputStyle}
            />
          </label>

          <button
            onClick={handleCapture}
            disabled={busy || !imu || !name.trim()}
            title={imu ? 'Snapshot current IMU angle + axis-b motor position' : 'Waiting for IMU data'}
            style={{ ...btnPrimary, background: '#8a4a4a', marginBottom: 10 }}
          >
            Teach angle
          </button>

          {targets.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {targets.map((t) => (
                <div
                  key={t.checkpoint_name}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '4px 6px', background: '#0d0d1a',
                    border: '1px solid #2a2a44', borderRadius: 4, fontSize: 11,
                  }}
                >
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <strong>{t.checkpoint_name}</strong>
                    {t.label && <span style={{ color: '#888' }}> — {t.label}</span>}
                  </span>
                  <span style={{ color: '#aaa', fontFamily: 'monospace', fontSize: 10 }}>
                    {t.active_angle}
                    {t.target_angle_deg >= 0 ? ' +' : ' '}
                    {t.target_angle_deg.toFixed(2)}°
                    &nbsp;@{t.motor_position.toFixed(0)}
                  </span>
                  <button
                    onClick={() => handleDelete(t.checkpoint_name)}
                    disabled={busy}
                    title="Delete"
                    style={{
                      padding: '2px 6px', background: '#522', border: '1px solid #744',
                      borderRadius: 3, color: '#faa', fontSize: 10, cursor: 'pointer',
                    }}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}
