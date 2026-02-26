# Gunicorn configuration for mysecond-web.
# Worker: single gthread worker with 16 threads.
# The worker_abort hook is intentionally NOT present:
# gunicorn handles worker timeouts by replacing the worker automatically,
# so there is no need to kill the master process.
