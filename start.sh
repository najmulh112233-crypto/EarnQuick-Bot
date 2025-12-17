#!/bin/bash
# Initialize the database
python -c "from app import initialize_db; initialize_db()"
# Start the Gunicorn web server
gunicorn app:app -b 0.0.0.0:$PORT
