# Forytography Shop — Setup Guide

This sets up real, working checkout + secure file delivery for your site. Follow these in order.

---

## What you're building

- Your **GitHub Pages site** stays exactly as it is — just with watermarked previews and "Purchase" buttons added.
- A tiny **Cloudflare Worker** (free) handles two jobs: starting a Stripe payment, and — only after that payment is confirmed — handing over the real file.
- Your real, un-watermarked photos live in a **private Cloudflare R2 bucket** (free up to 10GB) — never in your public GitHub repo.

---

## Step 1 — Create a Stripe account

1. Go to stripe.com and sign up (free).
2. You'll start in **Test mode** (toggle top-right of the dashboard) — use this for everything below until you're ready to go live.
3. Go to **Developers → API keys**. Copy the **Secret key** (starts with `sk_test_...`). You'll need this in Step 4.

You don't need to create Products/Prices in the Stripe dashboard — the Worker creates a price on the fly for each sale based on the `PHOTOS` table in its code.

---

## Step 2 — Create a Cloudflare account + R2 bucket

1. Go to cloudflare.com and sign up (free).
2. In the dashboard sidebar, go to **R2 Object Storage** → **Create bucket**.
3. Name it exactly: `forytography-originals`
4. Once created, click into the bucket → **Upload** → upload all 10 files from the `r2-originals` folder I gave you.
5. After uploading, **move them into a folder called `originals/`** inside the bucket (or upload directly into that path) — the Worker code expects them at paths like `originals/DSC_0707.jpg`. Most R2 upload dialogs let you type the destination path directly.

Keep this bucket **private** (default) — do not enable public access on it. The Worker reaches it through a secure binding, not a public URL.

---

## Step 3 — Install Wrangler (Cloudflare's deploy tool)

On your computer, with Node.js installed:

```bash
npm install -g wrangler
wrangler login
```

This opens a browser window to connect Wrangler to your Cloudflare account.

---

## Step 4 — Configure and deploy the Worker

1. Take the `worker` folder I gave you and open it in a terminal.
2. Open `wrangler.toml` and update:
   - `SITE_URL` → your actual site URL (e.g. `https://forytography.com`)
   - `ALLOWED_ORIGIN` → same value
3. Set your Stripe secret key (this keeps it out of your code, which is important — never paste secret keys directly into files you might upload anywhere public):
   ```bash
   cd worker
   wrangler secret put STRIPE_SECRET_KEY
   ```
   Paste your `sk_test_...` key when prompted.
4. Deploy it:
   ```bash
   wrangler deploy
   ```
5. Wrangler will print a URL like:
   ```
   https://forytography-shop.yourname.workers.dev
   ```
   **Copy this URL** — you need it in Step 5.

---

## Step 5 — Point your site at the Worker

In two files, replace the placeholder Worker URL with your real one from Step 4:

- `index.html` — search for `WORKER_URL` near the bottom, inside the `<script>` tag
- `success.html` — same variable name, near the top of its `<script>` tag

Then re-upload these updated files to your GitHub repo (same as any other update).

---

## Step 6 — Test it for real (in Stripe test mode)

1. Visit your live site, hover a photo, click **Purchase**.
2. You'll land on Stripe's real checkout page. Use Stripe's official test card:
   - Card number: `4242 4242 4242 4242`
   - Any future expiry date, any 3-digit CVC, any ZIP
3. Complete checkout — you should land on your `success.html` page and see a working **Download Your Photo** button.
4. Confirm the downloaded file is the full-resolution, watermark-free version.

If something fails, check the **Cloudflare dashboard → Workers → your worker → Logs** tab for the error.

---

## Step 7 — Go live

1. In Stripe, flip the dashboard from **Test mode** to **Live mode** (top right).
2. Go to **Developers → API keys** again — copy the **Live** secret key (`sk_live_...`).
3. Update the Worker's secret with the live key:
   ```bash
   wrangler secret put STRIPE_SECRET_KEY
   ```
   (paste the live key this time — it overwrites the test one)
4. Stripe will also want some basic business details (bank account for payouts, etc.) before it lets you accept real payments — it'll prompt you for these under **Settings**.

That's it — real purchases will now go through, and you'll get paid out by Stripe on their normal payout schedule (typically a few business days).

---

## Step 8 — Set up private client galleries (senior photo delivery)

This adds a second feature to the same Worker: password-protected albums for delivering client photo shoots.

1. Create the KV namespace that stores album info:
   ```bash
   wrangler kv namespace create ALBUM_KV
   ```
   It prints an `id` — paste that into `wrangler.toml` where it says `PASTE_YOUR_KV_NAMESPACE_ID_HERE`.

2. Set your personal master passkey — this one key will unlock *every* client album for you, forever:
   ```bash
   wrangler secret put ALBUM_MASTER_KEY
   ```
   Pick something memorable but not guessable (e.g. a random short phrase).

3. Redeploy the Worker so it picks up the new routes:
   ```bash
   wrangler deploy
   ```

4. In `album.html`, update the `WORKER_URL` placeholder to your real Worker URL (same one from Step 4).

5. Upload `album.html` to your GitHub repo alongside `index.html`.

**To create a new client album**, after a shoot:
```bash
python3 create_client_album.py \
    --slug smith-seniors-2026 \
    --client "The Smith Family" \
    --photos ~/Desktop/smith-shoot/
```
This uploads the photos privately, generates a client passkey, and prints the link + passkey to send them. Your master passkey from step 2 also works on this link, so you can always check it yourself.

---

## Ongoing: adding a new photo for sale

1. Add a new entry to the `PHOTOS` object in `worker/src/index.js` (name, price, r2Key).
2. Upload the full-res file to your R2 bucket at that same key.
3. Redeploy the Worker: `wrangler deploy`
4. Add a watermarked preview + Purchase button to `index.html`'s gallery grid (copy an existing card block, update the image, caption, and `data-photo-id`) — or just run `publish_new_photos.py` if you've set up the Apple Photos automation.

---

## Selling fine art prints (Prodigi)

Every for-sale photo also has an "Order a Print" option — a 16x24" Hahnemühle Photo Rag
print ($79), fulfilled by [Prodigi](https://www.prodigi.com). This is currently wired to
Prodigi's **sandbox** environment (no real orders, nothing printed or charged).

**To go live:**
1. In your Prodigi dashboard, switch to Live mode and generate a **Live** API key
   (separate from your Sandbox key).
2. Update the secret:
   ```bash
   cd worker
   wrangler secret put PRODIGI_API_KEY
   ```
   (paste the Live key — it overwrites the sandbox one)
3. In `wrangler.toml`, change:
   ```toml
   PRODIGI_BASE_URL = "https://api.prodigi.com"
   ```
4. Redeploy: `wrangler deploy`

**Changing the print size, paper, or price:** edit `PRINT_SKU` and `PRINT_PRICE_CENTS`
near the top of `worker/src/index.js`. To offer more than one size/style, you'd extend
the print button in `index.html` to pass along a chosen SKU instead of always using
the single `PRINT_SKU` constant.

**If a print order fails after payment** (Prodigi API hiccup, missing shipping
address, etc.), you'll get an email at your `CONTACT_EMAIL` with the Stripe session
ID — check the Stripe dashboard for that session and either place the Prodigi order
manually or refund the customer.

---

Happy to walk through any of these steps in more detail, or help troubleshoot if something doesn't work as expected on the first deploy — this kind of setup often needs one or two small adjustments the first time through.
