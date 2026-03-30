import { useMemo, useState } from 'react'
import GridLayout, { type Layout, type LayoutItem } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import { CameraCard } from './CameraCard'
import { ControlPanel } from './ControlPanel'
import type { CameraStatus } from '../types'

interface Props {
  cameras: CameraStatus[]
  onRefresh: () => void
}

type LayoutPreset = '1x1' | '2x2' | '3x3' | '4x2'

const COLS = 12
const ROW_HEIGHT = 180

function presetLayouts(cameras: CameraStatus[], preset: LayoutPreset): LayoutItem[] {
  switch (preset) {
    case '1x1':
      return cameras.slice(0, 1).map((c) => ({ i: c.id, x: 0, y: 0, w: COLS, h: 4 }))
    case '2x2': {
      const colW = 6
      return cameras.slice(0, 4).map((c, idx) => ({
        i: c.id, x: (idx % 2) * colW, y: Math.floor(idx / 2) * 4, w: colW, h: 4,
      }))
    }
    case '3x3': {
      const colW = 4
      return cameras.slice(0, 9).map((c, idx) => ({
        i: c.id, x: (idx % 3) * colW, y: Math.floor(idx / 3) * 3, w: colW, h: 3,
      }))
    }
    case '4x2': {
      const colW = 3
      return cameras.slice(0, 8).map((c, idx) => ({
        i: c.id, x: (idx % 4) * colW, y: Math.floor(idx / 4) * 3, w: colW, h: 3,
      }))
    }
  }
}

function defaultLayout(cameras: CameraStatus[]): LayoutItem[] {
  return cameras.map((c, idx) => ({
    i: c.id, x: (idx % 2) * 6, y: Math.floor(idx / 2) * 4, w: 6, h: 4,
  }))
}

export function CameraGrid({ cameras, onRefresh }: Props) {
  const [layout, setLayout] = useState<LayoutItem[]>(() => defaultLayout(cameras))
  const [preset, setPreset] = useState<LayoutPreset | null>(null)
  const [maximizedId, setMaximizedId] = useState<string | null>(null)
  const [selectedCamera, setSelectedCamera] = useState<CameraStatus | null>(null)
  const [containerWidth, setContainerWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth - 20 : 1200
  )

  const effectiveLayout = useMemo((): LayoutItem[] => {
    if (maximizedId) return [{ i: maximizedId, x: 0, y: 0, w: COLS, h: 6 }]
    return layout
  }, [layout, maximizedId])

  const visibleCameras = maximizedId ? cameras.filter((c) => c.id === maximizedId) : cameras

  const applyPreset = (p: LayoutPreset) => {
    setPreset(p)
    setMaximizedId(null)
    setLayout(presetLayouts(cameras, p))
  }

  const handleLayoutChange = (newLayout: Layout) => setLayout([...newLayout])

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '8px 12px', background: '#111', borderBottom: '1px solid #333', fontFamily: 'monospace' }}>
        <span style={{ color: '#888', fontSize: 12, marginRight: 4 }}>Layout:</span>
        {(['1x1', '2x2', '3x3', '4x2'] as LayoutPreset[]).map((p) => (
          <button
            key={p}
            onClick={() => applyPreset(p)}
            style={{ padding: '4px 10px', fontSize: 12, border: '1px solid #444', borderRadius: 4, background: preset === p ? '#334' : '#222', color: preset === p ? '#adf' : '#aaa', cursor: 'pointer' }}
          >
            {p}
          </button>
        ))}
        <span style={{ marginLeft: 'auto', color: '#666', fontSize: 12 }}>
          {cameras.length} camera{cameras.length !== 1 ? 's' : ''}
        </span>
      </div>

      <div ref={(el) => { if (el) setContainerWidth(el.offsetWidth) }}>
        <GridLayout
          className="layout"
          layout={effectiveLayout}
          width={containerWidth}
          gridConfig={{ cols: COLS, rowHeight: ROW_HEIGHT, margin: [6, 6] as const }}
          dragConfig={{ enabled: !maximizedId, bounded: false, threshold: 3 }}
          resizeConfig={{ enabled: !maximizedId, handles: ['se'] as const }}
          onLayoutChange={handleLayoutChange}
        >
          {visibleCameras.map((camera) => (
            <div key={camera.id}>
              <CameraCard
                camera={camera}
                onSettings={setSelectedCamera}
                onRefresh={onRefresh}
                maximized={maximizedId === camera.id}
                onMaximize={() => setMaximizedId(camera.id)}
                onRestore={() => setMaximizedId(null)}
              />
            </div>
          ))}
        </GridLayout>
      </div>

      {selectedCamera && (
        <ControlPanel
          camera={selectedCamera}
          allCameras={cameras}
          onClose={() => setSelectedCamera(null)}
          onRefresh={onRefresh}
        />
      )}
    </div>
  )
}
