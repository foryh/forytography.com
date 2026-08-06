/**
 * Forytography Shop + Client Galleries — Cloudflare Worker
 * -----------------------------------------------------------
 * Public shop:
 *   1. POST /create-checkout   -> creates a Stripe Checkout Session for a photo, returns the payment URL
 *   2. GET  /get-download      -> verifies payment with Stripe, then streams the real file from R2
 *
 * Private client galleries (senior photo delivery, etc.):
 *   3. POST /album/access      -> checks a passkey against an album, returns the list of photos if valid
 *   4. GET  /album/photo       -> streams one photo from R2, only if the passkey (sent as a header) checks out
 *   5. POST /clients/access    -> resolves a passkey: master key -> full album directory,
 *                                  a client's own passkey -> just their album's slug. Powers the
 *                                  Client Work page, so entering any valid passkey there gets you
 *                                  straight to the right gallery.
 *
 * Every client album has its own passkey. Your ALBUM_MASTER_KEY secret works as a passkey
 * for EVERY album, so you always have access without needing to remember each client's key.
 */

const PHOTOS = {
  "DSC_0707": { name: "Sawtooth Range at Dusk",        priceCents: 1500, r2Key: "originals/DSC_0707.jpg" },
  "DSC_0720": { name: "Sawtooth Valley Sunset",        priceCents: 1500, r2Key: "originals/DSC_0720.jpg" },
  "DSC_9351": { name: "High Desert Ridgeline",         priceCents: 1500, r2Key: "originals/DSC_9351.jpg" },
  "DSC_9546": { name: "Marsh Life",                    priceCents: 1500, r2Key: "originals/DSC_9546.jpg" },
  "DSC_4872": { name: "Ridgeline Layers",              priceCents: 1500, r2Key: "originals/DSC_4872.jpg" },
  "DSC_4784": { name: "Backcountry Camp",              priceCents: 1500, r2Key: "originals/DSC_4784.jpg" },
  "DSC_4983": { name: "Evening Light",                 priceCents: 1500, r2Key: "originals/DSC_4983.jpg" },
  "DSC_5497": { name: "Foggy Morning Portrait",        priceCents: 2500, r2Key: "originals/DSC_5497.jpg" },
  "DSC_2218": { name: "Boise Gems - Drummer",          priceCents: 2000, r2Key: "originals/DSC_2218.jpg" },
  "DSC_2266": { name: "Boise Gems - Horn Player",       priceCents: 2000, r2Key: "originals/DSC_2266.jpg" }
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = corsHeaders(env.ALLOWED_ORIGIN);

    // Preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    try {
      if (url.pathname === "/create-checkout" && request.method === "POST") {
        return await createCheckout(request, env, cors);
      }
      if (url.pathname === "/get-download" && request.method === "GET") {
        return await getDownload(url, env, cors);
      }
      if (url.pathname === "/album/access" && request.method === "POST") {
        return await albumAccess(request, env, cors);
      }
      if (url.pathname === "/album/photo" && request.method === "GET") {
        return await albumPhoto(request, url, env, cors);
      }
      if (url.pathname === "/clients/access" && request.method === "POST") {
        return await clientsAccess(request, env, cors);
      }
      return new Response("Not found", { status: 404, headers: cors });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500,
        headers: { ...cors, "Content-Type": "application/json" }
      });
    }
  }
};

function corsHeaders(allowedOrigin) {
  return {
    "Access-Control-Allow-Origin": allowedOrigin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Album-Key"
  };
}

// ---- Hash a passkey with SHA-256 so raw passkeys are never stored ----
async function hashKey(key) {
  const data = new TextEncoder().encode(key.trim());
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, "0")).join("");
}

// ---- Check whether a submitted key is your personal master key ----
function isMasterKey(env, submittedKey) {
  return Boolean(
    submittedKey && env.ALBUM_MASTER_KEY && submittedKey.trim() === env.ALBUM_MASTER_KEY.trim()
  );
}

// ---- Check a submitted passkey against an album's stored hash OR your master key ----
async function checkAlbumAccess(env, slug, submittedKey) {
  if (!submittedKey) return { ok: false, album: null };

  // Master key always works, for any album
  if (isMasterKey(env, submittedKey)) {
    const albumRaw = await env.ALBUM_KV.get(`album:${slug}`);
    if (!albumRaw) return { ok: false, album: null };
    return { ok: true, album: JSON.parse(albumRaw) };
  }

  const albumRaw = await env.ALBUM_KV.get(`album:${slug}`);
  if (!albumRaw) return { ok: false, album: null };
  const album = JSON.parse(albumRaw);

  const submittedHash = await hashKey(submittedKey);
  if (submittedHash === album.passkeyHash) {
    return { ok: true, album };
  }
  return { ok: false, album: null };
}

// ---- Check the passkey and, if valid, return the album's photo list ----
async function albumAccess(request, env, cors) {
  const { slug, key } = await request.json();
  if (!slug || !key) {
    return new Response(JSON.stringify({ error: "Missing slug or key" }), {
      status: 400, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const { ok, album } = await checkAlbumAccess(env, slug, key);
  if (!ok) {
    return new Response(JSON.stringify({ error: "Incorrect passkey" }), {
      status: 403, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  return new Response(JSON.stringify({
    ok: true,
    clientName: album.clientName,
    photos: album.photoKeys.map(k => ({
      file: k.split("/").pop(),
    }))
  }), { headers: { ...cors, "Content-Type": "application/json" } });
}

// ---- Stream one photo from R2, only after re-checking the passkey ----
async function albumPhoto(request, url, env, cors) {
  const slug = url.searchParams.get("slug");
  const file = url.searchParams.get("file");
  const key = request.headers.get("X-Album-Key");

  if (!slug || !file || !key) {
    return new Response(JSON.stringify({ error: "Missing slug, file, or key" }), {
      status: 400, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const { ok, album } = await checkAlbumAccess(env, slug, key);
  if (!ok) {
    return new Response(JSON.stringify({ error: "Incorrect passkey" }), {
      status: 403, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const r2Key = album.photoKeys.find(k => k.endsWith(`/${file}`));
  if (!r2Key) {
    return new Response(JSON.stringify({ error: "Photo not in this album" }), {
      status: 404, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const object = await env.PHOTOS_BUCKET.get(r2Key);
  if (!object) {
    return new Response(JSON.stringify({ error: "File missing from storage" }), {
      status: 404, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const download = url.searchParams.get("download") === "1";
  return new Response(object.body, {
    headers: {
      ...cors,
      "Content-Type": "image/jpeg",
      ...(download ? { "Content-Disposition": `attachment; filename="${file}"` } : {})
    }
  });
}

// ---- Read every album out of KV, as {slug, entry} pairs ----
async function listAllAlbumEntries(env) {
  const entries = [];
  let cursor;
  do {
    const page = await env.ALBUM_KV.list({ prefix: "album:", cursor });
    for (const item of page.keys) {
      const raw = await env.ALBUM_KV.get(item.name);
      if (!raw) continue;
      entries.push({ slug: item.name.slice("album:".length), album: JSON.parse(raw) });
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return entries;
}

// ---- Resolve a passkey typed into the Client Work page ----
// Master key -> the full album directory. A client's own passkey -> just their slug,
// so clicking "Client Work" and entering their passkey takes them straight to their gallery.
async function clientsAccess(request, env, cors) {
  const { key } = await request.json();
  if (!key) {
    return new Response(JSON.stringify({ error: "Missing key" }), {
      status: 400, headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const entries = await listAllAlbumEntries(env);

  if (isMasterKey(env, key)) {
    const albums = entries.map(({ slug, album }) => ({
      slug,
      clientName: album.clientName,
      photoCount: album.photoKeys.length,
      coverFile: album.photoKeys[0] ? album.photoKeys[0].split("/").pop() : null
    }));
    return new Response(JSON.stringify({ ok: true, role: "master", albums }), {
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const submittedHash = await hashKey(key);
  const match = entries.find(({ album }) => album.passkeyHash === submittedHash);
  if (match) {
    return new Response(JSON.stringify({ ok: true, role: "client", slug: match.slug }), {
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  return new Response(JSON.stringify({ error: "Incorrect passkey" }), {
    status: 403, headers: { ...cors, "Content-Type": "application/json" }
  });
}

// ---- Create a Stripe Checkout Session for one photo ----
async function createCheckout(request, env, cors) {
  const { photoId } = await request.json();
  const photo = PHOTOS[photoId];

  if (!photo) {
    return new Response(JSON.stringify({ error: "Unknown photo" }), {
      status: 400,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const siteUrl = env.SITE_URL; // e.g. https://forytography.com

  const body = new URLSearchParams({
    mode: "payment",
    "line_items[0][price_data][currency]": "usd",
    "line_items[0][price_data][product_data][name]": `${photo.name} — Digital Download`,
    "line_items[0][price_data][unit_amount]": String(photo.priceCents),
    "line_items[0][quantity]": "1",
    success_url: `${siteUrl}/success.html?session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${siteUrl}/index.html`,
    "metadata[photoId]": photoId,
    "managed_payments[enabled]": "false"
  });

  const stripeRes = await fetch("https://api.stripe.com/v1/checkout/sessions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.STRIPE_SECRET_KEY}`,
      "Content-Type": "application/x-www-form-urlencoded"
    },
    body: body.toString()
  });

  const session = await stripeRes.json();

  if (!stripeRes.ok) {
    return new Response(JSON.stringify({ error: session.error?.message || "Stripe error" }), {
      status: 400,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  return new Response(JSON.stringify({ url: session.url }), {
    headers: { ...cors, "Content-Type": "application/json" }
  });
}

// ---- Verify payment, then stream the real file from R2 ----
async function getDownload(url, env, cors) {
  const sessionId = url.searchParams.get("session_id");
  if (!sessionId) {
    return new Response(JSON.stringify({ error: "Missing session_id" }), {
      status: 400,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const stripeRes = await fetch(`https://api.stripe.com/v1/checkout/sessions/${sessionId}`, {
    headers: { "Authorization": `Bearer ${env.STRIPE_SECRET_KEY}` }
  });
  const session = await stripeRes.json();

  if (!stripeRes.ok) {
    return new Response(JSON.stringify({ error: "Could not verify payment" }), {
      status: 400,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  if (session.payment_status !== "paid") {
    return new Response(JSON.stringify({ error: "Payment not completed" }), {
      status: 402,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const photoId = session.metadata?.photoId;
  const photo = PHOTOS[photoId];
  if (!photo) {
    return new Response(JSON.stringify({ error: "Photo not found" }), {
      status: 404,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  const object = await env.PHOTOS_BUCKET.get(photo.r2Key);
  if (!object) {
    return new Response(JSON.stringify({ error: "File missing from storage" }), {
      status: 404,
      headers: { ...cors, "Content-Type": "application/json" }
    });
  }

  return new Response(object.body, {
    headers: {
      ...cors,
      "Content-Type": "image/jpeg",
      "Content-Disposition": `attachment; filename="${photoId}-forytography.jpg"`
    }
  });
}
