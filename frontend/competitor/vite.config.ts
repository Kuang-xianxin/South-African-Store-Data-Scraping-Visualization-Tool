import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export const CSS_ASSET_CACHE_EPOCH = "css-mime-v2";

type FrontendAssetInfo = {
  name?: string;
  names?: readonly string[];
};

export function frontendAssetFileName(assetInfo: FrontendAssetInfo): string {
  const names = [assetInfo.name, ...(assetInfo.names ?? [])];
  const isCss = names.some((name) => name?.toLowerCase().endsWith(".css"));
  return isCss
    ? `assets/[name]-[hash]-${CSS_ASSET_CACHE_EPOCH}[extname]`
    : "assets/[name]-[hash][extname]";
}

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        assetFileNames: frontendAssetFileName,
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8501",
    },
  },
});
