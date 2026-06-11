import React from "react";
import { Composition } from "remotion";
import { ExampleComposition } from "./Composition";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="ExampleComposition"
        component={ExampleComposition}
        durationInFrames={90}
        fps={30}
        width={1280}
        height={720}
      />
    </>
  );
};
