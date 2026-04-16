import type { IMUData } from '../types'

interface Props {
  imu: IMUData
}

/** Formats a signed angle to a fixed-width string, e.g. "+14.7°" or " -2.1°" */
function fmtAngle(deg: number): string {
  const sign = deg >= 0 ? '+' : ''
  return `${sign}${deg.toFixed(1)}°`
}

/**
 * Overlay displayed in the bottom-left corner of the camera stream.
 * Shows live roll and pitch angles from the camera's onboard IMU.
 */
export function ImuOverlay({ imu }: Props) {
  return (
    <div
      style={{
        position: 'absolute',
        bottom: 6,
        left: 6,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        padding: '4px 8px',
        background: 'rgba(0, 10, 20, 0.72)',
        backdropFilter: 'blur(3px)',
        borderRadius: 4,
        border: '1px solid rgba(0, 200, 255, 0.25)',
        pointerEvents: 'none',
        userSelect: 'none',
      }}
    >
      <div style={rowStyle}>
        <span style={labelStyle}>Roll </span>
        <span style={valueStyle}>{fmtAngle(imu.roll_deg)}</span>
      </div>
      <div style={rowStyle}>
        <span style={labelStyle}>Pitch</span>
        <span style={valueStyle}>{fmtAngle(imu.pitch_deg)}</span>
      </div>
    </div>
  )
}

const rowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  gap: 6,
}

const labelStyle: React.CSSProperties = {
  fontFamily: 'monospace',
  fontSize: 10,
  color: 'rgba(0, 200, 255, 0.7)',
  letterSpacing: '0.05em',
  textTransform: 'uppercase',
  width: 30,
}

const valueStyle: React.CSSProperties = {
  fontFamily: 'monospace',
  fontSize: 13,
  fontWeight: 700,
  color: '#00cfff',
  minWidth: 58,
  textAlign: 'right',
}
