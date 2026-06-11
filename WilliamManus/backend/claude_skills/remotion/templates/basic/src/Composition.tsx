import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export const ExampleComposition: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const opacity = interpolate(frame, [0, 30], [0, 1], {
    extrapolateRight: "clamp",
  });

  const scale = spring({
    frame,
    fps,
    config: { damping: 12, stiffness: 200, mass: 0.5 },
  });

  const translateY = interpolate(frame, [0, 30], [50, 0], {
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#1a1a2e",
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      <div
        style={{
          opacity,
          transform: `scale(${scale}) translateY(${translateY}px)`,
          textAlign: "center",
        }}
      >
        <h1
          style={{
            color: "#e94560",
            fontSize: 72,
            fontFamily: "sans-serif",
            fontWeight: "bold",
            margin: 0,
          }}
        >
          Hello Remotion
        </h1>
        <p
          style={{
            color: "#ffffff",
            fontSize: 28,
            fontFamily: "sans-serif",
            marginTop: 16,
            opacity: interpolate(frame, [15, 45], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        >
          Programmatic Video Creation
        </p>
      </div>
    </AbsoluteFill>
  );
};
