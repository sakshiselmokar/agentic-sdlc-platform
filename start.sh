#!/bin/bash
# start.sh — One command to install deps and start the API

echo "Installing dependencies..."
pip install -r requirements.txt --quiet

echo "Starting Agentic SDLC Platform API..."
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
