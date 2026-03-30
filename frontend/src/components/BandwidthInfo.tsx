import { useEffect, useState } from 'react'
import { checkBandwidth, getBandwidthMatrix } from '../hooks/useCamera'
import type { BandwidthEstimate, BandwidthMatrix } from '../types'

interface Props {
  resolution: string
  quality: number
  fps: number
  numCameras: number      // enabled cameras only
  totalCameras: number    // all cameras (enabled + disabled)
  disabledCameras: number // cameras turned off to free bandwidth
  stereoMode: string
}

const RESOLUTIONS = ['4k', '1080p', '720p', '480p']

function utilizationColor(pct: number): string {
  if (pct > 100) return '#ff4444'
  if (pct > 80) return '#ffaa33'
  if (pct > 60) return '#ffcc44'
  return '#44cc66'
}

function fpsColor(maxFps: number, requested: number): string {
  if (maxFps === 0) return '#ff4444'
  if (maxFps < requested) return '#ffaa33'
  return '#44cc66'
}

const barContainerStyle: React.CSSProperties = {
  height: 6,
  borderRadius: 3,
  background: '#333',
  overflow: 'hidden',
  marginTop: 4,
}

function barFillStyle(pct: number): React.CSSProperties {
  return {
    height: '100%',
    width: `${Math.min(100, pct)}%`,
    background: utilizationColor(pct),
    borderRadius: 3,
    transition: 'width 0.3s, background 0.3s',
  }
}

export function BandwidthInfo({ resolution, quality, fps, numCameras, totalCameras, disabledCameras, stereoMode }: Props) {
  const [estimate, setEstimate] = useState<BandwidthEstimate | null>(null)
  const [matrix, setMatrix] = useState<BandwidthMatrix | null>(null)
  const [showMatrix, setShowMatrix] = useState(false)

  // Fetch live estimate when settings change
  useEffect(() => {
    const n = Math.max(1, numCameras)
    checkBandwidth(resolution, quality, fps, n, stereoMode)
      .then(setEstimate)
      .catch(() => {})
  }, [resolution, quality, fps, numCameras, stereoMode])

  // Fetch full matrix on expand
  useEffect(() => {
    if (showMatrix && !matrix) {
      getBandwidthMatrix().then(setMatrix).catch(() => {})
    }
  }, [showMatrix, matrix])

  if (!estimate) return null

  // Find max FPS for current resolution + camera count
  const currentMaxFps = matrix?.profiles.find(
    (p) =>
      p.resolution === resolution &&
      p.stereo_mode === stereoMode &&
      p.num_cameras === numCameras,
  )?.max_fps

  return (
    <div
      style={{
        background: estimate.feasible ? '#1a2a1a' : '#2a1a1a',
        border: `1px solid ${estimate.feasible ? '#2a4a2a' : '#4a2a2a'}`,
        borderRadius: 6,
        padding: '10px 12px',
        fontSize: 11,
        fontFamily: 'monospace',
        marginBottom: 8,
      }}
    >
      {/* Current estimate */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontWeight: 700, color: estimate.feasible ? '#66dd88' : '#ff6666' }}>
          {estimate.feasible ? 'Bandwidth OK' : 'Bandwidth EXCEEDED'}
        </span>
        <span style={{ color: utilizationColor(estimate.utilization_pct) }}>
          {estimate.utilization_pct}% used
        </span>
      </div>

      {/* Utilization bar */}
      <div style={barContainerStyle}>
        <div style={barFillStyle(estimate.utilization_pct)} />
      </div>

      {/* Details */}
      <div style={{ marginTop: 6, color: '#aaa', lineHeight: 1.6 }}>
        <div>
          Per camera: <span style={{ color: '#ddd' }}>{estimate.per_camera_mbps} Mbps</span>
          {' | '}
          Total: <span style={{ color: '#ddd' }}>{estimate.total_mbps} Mbps</span>
          {' / '}
          <span style={{ color: '#888' }}>{estimate.budget_mbps} Mbps</span>
        </div>
        {!estimate.feasible && currentMaxFps !== undefined && (
          <div style={{ color: '#ffaa33', marginTop: 2 }}>
            Max sustainable FPS at these settings: <strong>{currentMaxFps}</strong>
          </div>
        )}
        {!estimate.feasible && currentMaxFps === undefined && (
          <div style={{ color: '#ffaa33', marginTop: 2 }}>
            Reduce resolution, FPS, or number of cameras.
          </div>
        )}
      </div>

      {/* Disabled cameras info */}
      {disabledCameras > 0 && (
        <div style={{ color: '#88aacc', fontSize: 10, marginTop: 4 }}>
          {disabledCameras} camera{disabledCameras > 1 ? 's' : ''} disabled
          — bandwidth calculated for {numCameras} active stream{numCameras !== 1 ? 's' : ''} only.
        </div>
      )}
      {!estimate.feasible && disabledCameras === 0 && totalCameras > 1 && (
        <div style={{ color: '#88aacc', fontSize: 10, marginTop: 4 }}>
          Tip: disable unused cameras (⏻ button on each card) to free bandwidth.
        </div>
      )}

      {/* Quality note */}
      <div style={{ color: '#887744', fontSize: 10, marginTop: 4 }}>
        Note: OAK-D hardware encoder produces constant-size frames — MJPEG quality
        affects visual quality only, not bandwidth. Adjust resolution or FPS to
        control bandwidth.
      </div>

      {/* Toggle matrix button */}
      <button
        onClick={() => setShowMatrix((v) => !v)}
        style={{
          background: 'none',
          border: 'none',
          color: '#6688cc',
          cursor: 'pointer',
          fontSize: 11,
          padding: '4px 0',
          marginTop: 4,
          textDecoration: 'underline',
        }}
      >
        {showMatrix ? 'Hide' : 'Show'} bandwidth reference table
      </button>

      {/* Bandwidth matrix table */}
      {showMatrix && matrix && (
        <div style={{ marginTop: 8, overflowX: 'auto' }}>
          <div style={{ color: '#888', marginBottom: 6 }}>
            Max FPS by resolution — PoE {matrix.poe_bandwidth_gbps} Gbps
            ({matrix.usable_bandwidth_mbps} Mbps usable)
            {matrix.measured && <span style={{ color: '#66cc66' }}> [measured on hardware]</span>}
            {!matrix.measured && <span style={{ color: '#ccaa44' }}> [estimated]</span>}
          </div>

          {/* Compact table: resolution x camera count */}
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: 11,
            }}
          >
            <thead>
              <tr style={{ borderBottom: '1px solid #444' }}>
                <th style={{ textAlign: 'left', padding: '4px 6px', color: '#888' }}>Resolution</th>
                <th style={{ textAlign: 'center', padding: '4px 6px', color: '#888' }}>Mbps/cam</th>
                <th style={{ textAlign: 'center', padding: '4px 6px', color: '#aaccff' }}>1 cam</th>
                <th style={{ textAlign: 'center', padding: '4px 6px', color: '#aaccff' }}>2 cam</th>
                <th style={{ textAlign: 'center', padding: '4px 6px', color: '#aaccff' }}>3 cam</th>
                <th style={{ textAlign: 'center', padding: '4px 6px', color: '#aaccff' }}>4 cam</th>
              </tr>
            </thead>
            <tbody>
              {RESOLUTIONS.map((res) => {
                // Get max FPS for each camera count (quality is irrelevant, pick first)
                const getMaxFps = (nCams: number) => {
                  const p = matrix.profiles.find(
                    (pr) =>
                      pr.resolution === res &&
                      pr.num_cameras === nCams &&
                      pr.stereo_mode === stereoMode,
                  )
                  return p?.max_fps ?? 0
                }
                const getMbps = () => {
                  const p = matrix.profiles.find(
                    (pr) =>
                      pr.resolution === res &&
                      pr.num_cameras === 1 &&
                      pr.stereo_mode === stereoMode,
                  )
                  return p ? p.per_camera_mbps_at_max : 0
                }
                const isCurrentRow = res === resolution

                return (
                  <tr
                    key={res}
                    style={{
                      borderBottom: '1px solid #222',
                      background: isCurrentRow ? 'rgba(80,100,180,0.15)' : undefined,
                    }}
                  >
                    <td style={{
                      padding: '4px 6px',
                      color: isCurrentRow ? '#fff' : '#ccc',
                      fontWeight: isCurrentRow ? 700 : 400,
                    }}>
                      {res}
                    </td>
                    <td style={{ textAlign: 'center', padding: '4px 6px', color: '#999' }}>
                      {getMbps() > 0 ? `~${Math.round(getMbps())}` : '?'}
                    </td>
                    {[1, 2, 3, 4].map((nCams) => {
                      const mfps = getMaxFps(nCams)
                      const isCurrentCell = isCurrentRow && nCams === numCameras
                      return (
                        <td
                          key={nCams}
                          style={{
                            textAlign: 'center',
                            padding: '4px 6px',
                            color: fpsColor(mfps, fps),
                            fontWeight: isCurrentCell ? 900 : 400,
                            background: isCurrentCell ? 'rgba(100,120,200,0.3)' : undefined,
                            borderRadius: isCurrentCell ? 3 : 0,
                          }}
                        >
                          {mfps === 0 ? '---' : mfps >= 60 ? '60+' : mfps}
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>

          <div style={{ color: '#666', fontSize: 10, marginTop: 6, lineHeight: 1.5 }}>
            Values = max FPS within PoE budget.{' '}
            <span style={{ color: '#44cc66' }}>Green</span> = fits |{' '}
            <span style={{ color: '#ffaa33' }}>Yellow</span> = below your FPS |{' '}
            <span style={{ color: '#ff4444' }}>Red</span> = not feasible.
            Quality column removed — hardware encoder output size is constant.
          </div>
        </div>
      )}
    </div>
  )
}
