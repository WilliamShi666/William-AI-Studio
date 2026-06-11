module.exports = {
  apps: [{
    name: 'wm-frontend',
    script: 'node_modules/.bin/next',
    args: 'start --port 3000 --hostname 127.0.0.1',
    cwd: '/path/to/williams-ai-studio/WilliamManus/frontend',
    autorestart: true,
    max_memory_restart: '512M',
    restart_delay: 3000,
    max_restarts: 50,
    min_uptime: '10s',
    env: {
      NODE_ENV: 'production',
      PORT: '3000',
    },
  }],
};
