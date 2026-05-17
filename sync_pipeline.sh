#!/bin/bash

# Exit immediately if any command fails
set -e

echo "=== Starting Portfolio Sync Pipeline ==="

# 1. (Future step) Fetch data from IBKR API here
echo "Step 1: Checking for new transaction data..."

# 2. Convert Portfolio Performance XML to SQLite
echo "Step 2: Converting .portfolio XML to temporary SQLite database..."
if [ -f /data/my_wallet.portfolio ]; then
    python ppxml2db.py portfolio2sqlite /data/my_wallet.portfolio /app/temp.db
else
    echo "ERROR: /data/my_wallet.portfolio not found! Please place your master file in the volume."
    exit 1
fi

# 3. Run the Python appender (currently a placeholder)
echo "Step 3: Running transaction injection script..."
python /app/append_transactions.py

# 4. Convert the SQLite database back to the original .portfolio file
echo "Step 4: Compiling SQLite back into .portfolio format..."
python ppxml2db.py sqlite2portfolio /app/temp.db /data/my_wallet.portfolio

# 5. Clean up the temporary database
rm /app/temp.db

echo "=== Pipeline Completed Successfully ==="