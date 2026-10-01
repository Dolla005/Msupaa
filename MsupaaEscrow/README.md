# MsupaaEscrow

MsupaaEscrow is a working full-stack escrow transaction management system built with **Flask + SQLite**.

It includes:
- Buyer / Seller / Administrator accounts
- Role-based access
- Seller search and selection
- Escrow order creation
- Test funding into **HELD** status
- Shipping and delivery tracking
- Buyer confirmation and release
- Automatic release timer
- Dispute opening
- Administrator refund/release resolution
- Notifications
- Transaction/audit history
- Reports and CSV export

## Important payment note

Buyer, seller and administrator accounts are real application accounts stored in the database. The current payment step is still a **test payment flow** and does not move real money. Real escrow payments require integration with an appropriate payment/escrow provider, signed webhooks/callbacks, secure production infrastructure, and any required legal/compliance arrangements.

## Run on Windows

Open Command Prompt in the `MsupaaEscrow` folder:

```bat
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
py app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Create your own accounts

Buyer and Seller accounts are created from the website using **Create account**. No test users are automatically created.

To create the first Administrator account, stop the running server and use:

```bat
.venv\Scripts\activate
flask --app app create-admin
```

Enter your own administrator name, email, and password when prompted.

## Test the transaction flow with your own users

1. Register a Seller account.
2. Register a Buyer account using a different email address.
3. Sign in as the Buyer and find/select the Seller.
4. Create a new escrow order.
5. Fund it using the built-in test payment flow.
6. Sign in as the Seller and mark it as shipped.
7. Sign back in as the Buyer and confirm delivery, or open a dispute.
8. If a dispute is opened, sign in with your Administrator account and resolve it.
9. Check **Reports** and **Notifications** to verify the transaction history.

## Database

SQLite database location:

```text
instance/msupaaescrow.db
```

To reset all local users, orders, disputes and reports, stop the server and run `reset_database.bat`, or delete that database file. The next start will create a clean empty database.

## Auto-release

The default auto-release timer is 3 days after the seller marks an order as shipped.

To change it in Command Prompt:

```bat
set AUTO_RELEASE_DAYS=1
py app.py
```

The current development build checks for due releases when the application receives a request.

## Before a real production launch

Recommended additions include:
- M-Pesa/card/mobile-money or licensed escrow-provider integration
- Signed payment callbacks/webhooks
- PostgreSQL or another managed production database
- HTTPS
- Email/SMS notifications
- Background job worker for release timers
- File/evidence uploads for disputes
- KYC/AML and regulatory requirements where applicable
- Production secrets management
- Automated tests, backups, monitoring and cloud deployment
