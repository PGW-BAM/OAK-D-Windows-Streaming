import { useKreuzstoss } from '../contexts/KreuzstossContext'
import type { KreuzstossMode } from '../hooks/useCamera'

interface Props {
  cameraCount: number
  mode?: KreuzstossMode
}

const LABELS: Record<KreuzstossMode, { idle: string; running: string; tooltip: string }> = {
  full: {
    idle: 'KREUZSTOSS PROGRAMM',
    running: 'KREUZSTOSS LÄUFT…',
    tooltip: 'Run the full Kreuzstoss programme (4K + 1080p video on both cameras)',
  },
  simple: {
    idle: 'KREUZSTOSS SIMPLE',
    running: 'KREUZSTOSS SIMPLE LÄUFT…',
    tooltip: 'Bandwidth-safe variant: 1080p video + 4K snapshot on both cameras',
  },
}

export function KreuzstossButton({ cameraCount, mode = 'full' }: Props) {
  const { running, openPanel } = useKreuzstoss()
  const enoughCameras = cameraCount >= 2
  const disabled = !enoughCameras
  const labels = LABELS[mode]
  const label = running ? labels.running : labels.idle

  let title = labels.tooltip
  if (!enoughCameras) title = 'Connect 2 cameras first'
  if (running) title = 'Kreuzstoss is currently running — click to view progress'

  // Visually distinguish: full = orange, simple = teal.
  const idleBg = mode === 'simple' ? '#066' : '#a40'
  const idleBorder = mode === 'simple' ? '#4cf' : '#fc4'

  return (
    <button
      onClick={() => openPanel(mode)}
      disabled={disabled}
      title={title}
      className={`kreuzstoss-btn ${running ? 'running' : 'idle'}`}
      style={{
        height: 56,
        padding: '0 28px',
        fontSize: 18,
        fontWeight: 800,
        letterSpacing: 1.2,
        textTransform: 'uppercase',
        background: running ? '#552' : idleBg,
        color: '#fff',
        border: `2px solid ${idleBorder}`,
        borderRadius: 8,
        cursor: disabled ? 'not-allowed' : 'pointer',
        fontFamily: 'monospace',
        marginLeft: mode === 'full' ? 'auto' : 8,
      }}
    >
      {label}
    </button>
  )
}
