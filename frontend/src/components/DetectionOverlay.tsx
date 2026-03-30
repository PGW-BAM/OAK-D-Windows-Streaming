import { useEffect, useRef } from 'react'
import type { Detection } from '../types'

// Colour palette — cycles through hue values per class id
const classColor = (classId: number) => `hsl(${(classId * 47) % 360}, 90%, 55%)`

interface Props {
  detection: Detection | null
  width: number
  height: number
}

export function DetectionOverlay({ detection, width, height }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    ctx.clearRect(0, 0, width, height)
    if (!detection || detection.boxes.length === 0) return

    for (const box of detection.boxes) {
      const x = box.x1 * width
      const y = box.y1 * height
      const w = (box.x2 - box.x1) * width
      const h = (box.y2 - box.y1) * height
      const color = classColor(box.class_id)

      ctx.strokeStyle = color
      ctx.lineWidth = 2
      ctx.strokeRect(x, y, w, h)

      const label = `${box.label} ${(box.confidence * 100).toFixed(0)}%`
      ctx.font = 'bold 12px monospace'
      const textW = ctx.measureText(label).width + 8
      ctx.fillStyle = color
      ctx.fillRect(x - 1, y - 18, textW, 18)
      ctx.fillStyle = '#000'
      ctx.fillText(label, x + 3, y - 5)
    }
  }, [detection, width, height])

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
      }}
    />
  )
}
