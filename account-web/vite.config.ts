import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

// 开发时后端的 KUKEAUTH_ISSUER 配成 http://localhost:5175：代理不改 Host，页面和接口算同一个来源
const backend = {target: process.env.KUKEAUTH_BACKEND ?? 'http://127.0.0.1:8000', changeOrigin: false};

export default defineConfig({
    plugins: [react()],
    server: {
        port: 5175,
        proxy: {'/account/api': backend, '/oauth': backend, '/.well-known': backend, '/uploads': backend}
    }
});
