/**
 * PM2 생태계 설정
 * 
 * Claude API 키 설정:
 *   ANTHROPIC_API_KEY 환경변수를 설정하거나
 *   아래 env 블록에 직접 입력하세요.
 *
 * 실행:
 *   pm2 start ecosystem.config.cjs
 *   ANTHROPIC_API_KEY=sk-ant-... pm2 start ecosystem.config.cjs
 */
module.exports = {
  apps: [
    {
      name: 'blind-verify',
      script: 'python3',
      args: '-m uvicorn main:app --host 0.0.0.0 --port 3000 --workers 1',
      cwd: '/home/user/webapp/backend',
      env: {
        PYTHONPATH: '/home/user/webapp/backend',
        PYTHONUNBUFFERED: '1',
        // Claude API 키: 여기에 직접 입력하거나 환경변수로 전달
        ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY || '',
        CLAUDE_MODEL: process.env.CLAUDE_MODEL || 'claude-3-5-sonnet-20241022',
        AUTO_DELETE_MIN: process.env.AUTO_DELETE_MIN || '30',
        MAX_FILE_SIZE_MB: process.env.MAX_FILE_SIZE_MB || '100',
        OCR_ENABLED: process.env.OCR_ENABLED || 'true',
      },
      watch: false,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      error_file: '/home/user/webapp/logs/pm2-error.log',
      out_file: '/home/user/webapp/logs/pm2-out.log',
    },
  ],
};
