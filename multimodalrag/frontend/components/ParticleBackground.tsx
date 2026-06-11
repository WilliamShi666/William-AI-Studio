import { useCallback } from "react";
import Particles from "react-tsparticles";
import { loadSlim } from "tsparticles-slim";
import type { Container, Engine } from "tsparticles-engine";

export function ParticleBackground() {
  const particlesInit = useCallback(async (engine: Engine) => {
    await loadSlim(engine);
  }, []);

  const particlesLoaded = useCallback(async (_container: Container | undefined) => {
    // container is available here if needed
  }, []);

  return (
    <Particles
      id="tsparticles"
      init={particlesInit}
      loaded={particlesLoaded}
      className="fixed inset-0 pointer-events-none z-0"
      options={{
        fpsLimit: 60, // Limit FPS to save battery
        particles: {
          number: {
            value: 60, // Optimal density: 60-80
            density: {
              enable: true,
              value_area: 800,
            },
          },
          color: {
            value: "#38BDF8", // Sky Blue
          },
          shape: {
            type: "circle",
          },
          opacity: {
            value: 0.2, // Very low opacity for subtle effect
            random: true, // "Breathing" effect
          },
          size: {
            value: 3,
            random: true,
          },
          line_linked: {
            enable: true,
            distance: 150,
            color: "#38BDF8",
            opacity: 0.1, // Even lower opacity for lines
            width: 1,
          },
          move: {
            enable: true,
            speed: 0.6, // Very slow movement
            direction: "none",
            random: false,
            straight: false,
            out_mode: "out",
            bounce: false,
          },
        },
        interactivity: {
          detect_on: "canvas",
          events: {
            onhover: {
              enable: true,
              mode: "grab", // Magnetic connection effect
            },
            onclick: {
              enable: true,
              mode: "push",
            },
            resize: true,
          },
          modes: {
            grab: {
              distance: 140,
              line_linked: {
                opacity: 0.5, // Highlight connections on hover
              },
            },
            push: {
              particles_nb: 4,
            },
          },
        },
        retina_detect: true,
        fullScreen: {
          enable: false, // We handle positioning via CSS class
          zIndex: 0,
        },
      }}
    />
  );
}
