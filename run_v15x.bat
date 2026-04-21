@echo off
cd /d "c:\Users\lukeb\OneDrive\Desktop\New folder\PropBot"
python -u Scripts\v15x_universe_scan.py --symbols UK100 JP225 XAGUSD XBRUSD XTIUSD EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD EURGBP EURJPY EURCHF EURCAD EURAUD EURNZD GBPJPY GBPCAD AUDCAD AUDNZD NZDCAD CADJPY CHFJPY --out Results\v15x_universe_scan.json --report Docs\V15X_UNIVERSE_SCAN.md >> Results\v15x_full.log 2>&1
