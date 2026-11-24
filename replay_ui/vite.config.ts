import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into replay/app/web (committed), so the Windows app needs Python + PySide6 only, not Node.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../replay/app/web", emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 1200 },
});
