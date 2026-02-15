#!/bin/bash
# Stop the local dev stack
echo "Stopping dev stack..."
pkill -f "anvil" 2>/dev/null && echo "✓ Anvil stopped" || echo "  Anvil not running"
pkill -f "uvicorn.*main:app" 2>/dev/null && echo "✓ Gateway stopped" || echo "  Gateway not running"
echo "Done."
