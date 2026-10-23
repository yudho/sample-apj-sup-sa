import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// The app still reads process.env.REACT_APP_* (the Amplify and workshop config
// use those names), so expose exactly those keys at build time.
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), 'REACT_APP_');
  // Dev server sits behind the Code Editor's nginx at /app; the hosted build is served from /.
  const base = command === 'serve' ? '/app/' : '/';
  const processEnv = {
    ...env,
    NODE_ENV: mode === 'production' ? 'production' : 'development',
    PUBLIC_URL: base.replace(/\/$/, ''),
  };
  return {
    base,
    plugins: [react()],
    define: { 'process.env': JSON.stringify(processEnv) },
    envPrefix: 'REACT_APP_',
    server: { host: '127.0.0.1', port: 3001, strictPort: true, allowedHosts: true },
    preview: { host: '127.0.0.1', port: 3001, strictPort: true },
    build: { outDir: 'build', sourcemap: false },
  };
});
