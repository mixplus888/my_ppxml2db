#!/bin/bash

# Exit immediately if any command fails
set -e

echo "=== Starting Portfolio Sync Pipeline ==="

# 1. Fetch data from IBKR API here
echo "Step 1: Checking for new transaction data..."

# 2. Convert Portfolio Performance XML to SQLite
echo "Step 2: Converting .portfolio XML to temporary SQLite database..."
if [ -f /data/my_wallet.portfolio ]; then
    # 1. Ensure the database file is totally fresh and clear
    rm -f /app/temp.db
    
    # 2. Pre-initialize all native tables
    python3 /app/ppxml2db_init.py /app/temp.db
    
    # 3. Parse and load the XML records into the structured tables
    python3 ppxml2db.py /data/my_wallet.portfolio /app/temp.db
else
    echo "ERROR: /data/my_wallet.portfolio not found!"
    exit 1
fi

echo "=== DATABASE INGESTION AUDIT ==="
if [ -f /app/temp.db ]; then
    sqlite3 /app/temp.db "SELECT 'Securities found: ', COUNT(*) FROM security;"
    sqlite3 /app/temp.db "SELECT 'Accounts found:   ', COUNT(*) FROM account;"
    sqlite3 /app/temp.db "SELECT 'Transactions found:', COUNT(*) FROM xact;"
    sqlite3 /app/temp.db "SELECT 'Cross-entries found:', COUNT(*) FROM xact_cross_entry;"
else
    echo "Database file temp.db does not exist!"
fi
echo "================================"

# DIAGNOSTIC: Temporarily bypass Step 3 to verify it isn't wiping the database context
echo "Step 3: [BYPASSED FOR DIAGNOSTICS] Running transaction injection script..."
# python3 /app/append_transactions.py

# 4. Convert the SQLite database back to the original .portfolio file
echo "Step 4: Compiling SQLite back into .portfolio format..."
python3 db2ppxml.py /app/temp.db /data/my_wallet_synced.portfolio

# DIAGNOSTIC: Keep the database file alive after execution so we can inspect it
echo "Step 5: [BYPASSED FOR DIAGNOSTICS] Clean up the temporary database..."
# rm /app/temp.db

echo "=== Pipeline Completed Successfully ==="