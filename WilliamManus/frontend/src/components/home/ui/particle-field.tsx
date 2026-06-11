'use client';

import { useEffect, useRef } from 'react';

type Particle = {
  x: number;
  y: number;
  phase: number;
  period: number;
  radius: number;
  visibility: number;
};

/**
 * Canvas-based particle field that renders tiny breathing dots.
 * Dots flicker asynchronously using sine waves to avoid synchronized pulses.
 */
export function ParticleField() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const particlesRef = useRef<Particle[]>([]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;

    const resize = () => {
      const { innerWidth: width, innerHeight: height } = window;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // Density based on viewport area; keeps perf reasonable and visible.
      const density = Math.min(
        900,
        Math.max(260, Math.floor((width * height) / 5500)),
      );
      particlesRef.current = Array.from({ length: density }, () => {
        const edgeBias = () => {
          const t = Math.random();
          return Math.random() < 0.5 ? t * t : 1 - t * t;
        };
        const x = edgeBias() * width;
        const y = Math.random() * height;
        const edgeFactor = Math.abs(x - width / 2) / (width / 2);
        const visibility = 0.45 + 0.55 * Math.pow(edgeFactor, 1.15);

        return {
          x,
          y,
          radius: 0.5 + Math.random() * 0.5, // 1px - 2px diameter
          phase: Math.random() * Math.PI * 2,
          period: 3 + Math.random() * 4,
          visibility,
        };
      });
    };

    resize();
    window.addEventListener('resize', resize);

    const render = (timestamp: number) => {
      const width = canvas.width / dpr;
      const height = canvas.height / dpr;
      ctx.clearRect(0, 0, width, height);

      const minAlpha = 0.14;
      const maxAlpha = 0.6;
      const coreColor = '238, 243, 255';
      const glowColor = '96, 136, 210';
      const particles = particlesRef.current;
      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        const breathing =
          (Math.sin((timestamp / 1000) * ((Math.PI * 2) / p.period) + p.phase) +
            1) /
          2;
        const baseAlpha = minAlpha + breathing * (maxAlpha - minAlpha);
        const alpha = minAlpha + (baseAlpha - minAlpha) * p.visibility;
        ctx.fillStyle = `rgba(${glowColor}, ${alpha * 0.45})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.radius * 2.9, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = `rgba(${coreColor}, ${alpha})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
        ctx.fill();
      }
      animationRef.current = requestAnimationFrame(render);
    };

    animationRef.current = requestAnimationFrame(render);

    return () => {
      if (animationRef.current) cancelAnimationFrame(animationRef.current);
      window.removeEventListener('resize', resize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 -z-10"
      aria-hidden="true"
    />
  );
}
