"""Local runner script to test Kraken Micro-Trader from the command line.

Executes a single iteration of the Lambda handler in DRY_RUN mode using live market data.
"""

import json
import logging
import sys
from handler import lambda_handler

# Configure rich console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

if __name__ == "__main__":
    print("=" * 60)
    print("   Kraken Micro-Trader (Local Dry-Run Simulation)")
    print("=" * 60)

    result = lambda_handler()

    print("\nExecution Result:")
    print(json.dumps(result, indent=2))
    print("=" * 60)
