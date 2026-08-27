// State
let currentProduct = null;
let selectedSize = null;
let localProducts = [];

// DOM Elements
const viewCatalog = document.getElementById('view-catalog');
const viewProduct = document.getElementById('view-product');
const productGrid = document.getElementById('product-grid');
const productDetailContainer = document.getElementById('product-detail-container');
const cartCount = document.getElementById('cart-count');
const cartSidebar = document.getElementById('cart-sidebar');
const cartOverlay = document.getElementById('cart-overlay');
const cartItemsContainer = document.getElementById('cart-items');
const cartTotalAmount = document.getElementById('cart-total-amount');
const cartSubtotalAmount = document.getElementById('cart-subtotal-amount');
const checkoutMessage = document.getElementById('checkout-message');

// Search & Filter
const searchInput = document.getElementById('search-input');
const categoryFilter = document.getElementById('category-filter');

// Initialization
document.addEventListener('DOMContentLoaded', () => {
  loadCatalog();
  updateGatewayStatus();
  
  // Event Listeners
  document.getElementById('nav-catalog').addEventListener('click', (e) => {
    e.preventDefault();
    showCatalog();
  });
  
  const brandLogo = document.getElementById('brand-logo');
  if (brandLogo) {
    brandLogo.addEventListener('click', (e) => {
      e.preventDefault();
      showCatalog();
    });
  }
  
  document.getElementById('nav-cart').addEventListener('click', toggleCart);
  document.getElementById('close-cart').addEventListener('click', toggleCart);
  cartOverlay.addEventListener('click', toggleCart);
  document.getElementById('back-to-catalog').addEventListener('click', showCatalog);
  document.getElementById('btn-checkout').addEventListener('click', checkoutIntent);
  
  // Debounced search
  let searchTimeout;
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(loadCatalog, 300);
  });
  
  categoryFilter.addEventListener('change', loadCatalog);
});

// --- Gateway Status Check ---
async function updateGatewayStatus() {
  const pill = document.getElementById('gateway-status-pill');
  const text = document.getElementById('gateway-status-text');
  if (!pill || !text) return;

  try {
    const res = await fetch('/api/connection-status');
    const data = await res.json();
    
    if (data.connected) {
      pill.className = 'gateway-pill';
      text.textContent = '● Gateway Connected';
      pill.title = `Connected to Agent Commerce Gateway (${data.merchantId || 'Active'})`;
    } else {
      pill.className = 'gateway-pill disconnected';
      text.textContent = '○ Setup Gateway (/pay)';
      pill.title = 'Click to connect your Merchant API key';
    }
  } catch (err) {
    pill.className = 'gateway-pill disconnected';
    text.textContent = '○ Setup Gateway (/pay)';
  }
}

// --- API Calls & Rendering ---

async function loadCatalog() {
  const search = searchInput.value.trim();
  const category = categoryFilter.value;
  
  let url = '/api/products?';
  if (search) url += `search=${encodeURIComponent(search)}&`;
  if (category) url += `category=${encodeURIComponent(category)}`;
  
  try {
    const res = await fetch(url);
    const result = await res.json();
    localProducts = result.data || [];
    renderCatalog(localProducts);
  } catch (error) {
    console.error('Failed to load catalog:', error);
    productGrid.innerHTML = '<p style="color: #ef4444; padding: 2rem;">Failed to load collections. Please make sure the server is running.</p>';
  }
}

function renderCatalog(products) {
  productGrid.innerHTML = '';
  if (products.length === 0) {
    productGrid.innerHTML = `
      <div style="grid-column: 1/-1; text-align: center; padding: 4rem 2rem; background: white; border-radius: 16px; border: 1px dashed #cbd5e1;">
        <h3 style="font-size: 1.3rem; margin-bottom: 0.5rem;">No Footwear Found</h3>
        <p style="color: #64748b;">Try adjusting your search query or selecting a different category.</p>
      </div>
    `;
    return;
  }
  
  products.forEach(p => {
    const card = document.createElement('article');
    card.className = 'product-card';
    card.setAttribute('data-product-id', p.id);

    let stockBadgeClass = 'in-stock';
    let stockBadgeText = 'In Stock';
    if (p.availability === 'out_of_stock' || p.stock === 0) {
      stockBadgeClass = 'out-of-stock';
      stockBadgeText = 'Out of Stock';
    } else if (p.availability === 'low_stock' || p.stock <= 5) {
      stockBadgeClass = 'low-stock';
      stockBadgeText = `Only ${p.stock} Left`;
    }

    card.innerHTML = `
      <div class="product-image-wrap">
        <div class="card-badge-container">
          <span class="card-category-badge">${p.category}</span>
          <span class="card-stock-badge ${stockBadgeClass}">${stockBadgeText}</span>
        </div>
        <img src="${p.image}" alt="${p.name}" loading="lazy" onerror="this.src='https://placehold.co/600x600/1e293b/ffffff?text=${encodeURIComponent(p.name)}'">
      </div>
      <div class="product-info">
        <div class="product-rating">
          ★★★★★ <span>4.9 (48)</span>
        </div>
        <h3 class="product-title">${p.name}</h3>
        <p class="product-desc-snippet">${p.description}</p>
        <div class="product-bottom-row">
          <span class="product-price">₹${p.price.toLocaleString('en-IN')}</span>
          <button class="view-details-btn">
            <span>View Details</span>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>
          </button>
        </div>
      </div>
    `;

    card.addEventListener('click', () => loadProduct(p.id));
    productGrid.appendChild(card);
  });
}

async function loadProduct(id) {
  try {
    const res = await fetch(`/api/products/${id}`);
    const result = await res.json();
    if (result.data) {
      currentProduct = result.data;
      selectedSize = currentProduct.sizes[0] || "8";
      renderProductDetail(currentProduct);
      showProductDetail();
    }
  } catch (error) {
    console.error('Failed to load product:', error);
  }
}

function renderProductDetail(p) {
  let stockClass = 'in-stock';
  let stockText = 'In Stock (Ready to Ship)';
  let btnDisabled = '';
  
  if (p.availability === 'out_of_stock' || p.stock === 0) {
    stockClass = 'out-of-stock';
    stockText = 'Currently Out of Stock';
    btnDisabled = 'disabled';
  } else if (p.availability === 'low_stock' || p.stock <= 5) {
    stockClass = 'low-stock';
    stockText = `Low Stock — Only ${p.stock} units remaining`;
  }

  const sizesHtml = p.sizes.map(size => 
    `<button class="size-btn ${size === selectedSize ? 'selected' : ''}" data-size="${size}">${size}</button>`
  ).join('');

  productDetailContainer.innerHTML = `
    <div class="detail-image">
      <img src="${p.image}" alt="${p.name}" onerror="this.src='https://placehold.co/800x800/1e293b/ffffff?text=${encodeURIComponent(p.name)}'">
    </div>
    <div class="detail-info">
      <span class="detail-category-tag">${p.category} Collection</span>
      <h2>${p.name}</h2>
      <div class="product-rating" style="margin-bottom: 0.75rem;">
        ★★★★★ <span>4.9 (48 verified customer reviews)</span>
      </div>
      <div class="detail-price">₹${p.price.toLocaleString('en-IN')}</div>
      <p class="detail-desc">${p.description}</p>
      
      <div class="size-selector">
        <label>Select UK/IND Size</label>
        <div class="size-options">
          ${sizesHtml}
        </div>
      </div>
      
      <div class="card-stock-badge ${stockClass}" style="margin-bottom: 1.5rem; display: inline-flex; width: fit-content; font-size: 0.85rem; padding: 0.4rem 0.85rem;">
        ● ${stockText}
      </div>

      <button id="add-to-cart-btn" class="btn-primary" ${btnDisabled}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"></path><line x1="3" y1="6" x2="21" y2="6"></line><path d="M16 10a4 4 0 0 1-8 0"></path></svg>
        <span>Add to Bag</span>
      </button>

      <div class="detail-guarantees">
        <div class="guarantee-item">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>
          <span>5-Stage Policy Gated</span>
        </div>
        <div class="guarantee-item">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
          <span>Razorpay Instant Order</span>
        </div>
        <div class="guarantee-item">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
          <span>ACP 2026 Protocol</span>
        </div>
      </div>
    </div>
  `;

  // Size selection listener
  const sizeBtns = productDetailContainer.querySelectorAll('.size-btn');
  sizeBtns.forEach(btn => {
    btn.addEventListener('click', (e) => {
      sizeBtns.forEach(b => b.classList.remove('selected'));
      e.target.classList.add('selected');
      selectedSize = e.target.getAttribute('data-size');
    });
  });

  // Add to cart listener
  const addBtn = document.getElementById('add-to-cart-btn');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      const currentQty = getCartQty(p.id);
      updateCartAPI(p.id, currentQty + 1, selectedSize, true);
    });
  }
}

// --- Cart Operations ---

async function updateCartAPI(id, quantity, size = null, openDrawerAfter = false) {
  try {
    const res = await fetch('/api/cart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, quantity, size })
    });
    
    const result = await res.json();
    if (res.ok) {
      renderCart(result.data);
      if (openDrawerAfter) {
        openCart();
      }
    } else {
      alert(result.error || 'Failed to update cart');
    }
  } catch (err) {
    console.error('Cart update error:', err);
  }
}

function renderCart(cart) {
  const totalCount = cart.items.reduce((sum, item) => sum + item.quantity, 0);
  cartCount.textContent = totalCount;
  
  const formattedTotal = `₹${cart.total_amount.toLocaleString('en-IN')}`;
  cartTotalAmount.textContent = formattedTotal;
  if (cartSubtotalAmount) {
    cartSubtotalAmount.textContent = formattedTotal;
  }
  
  cartItemsContainer.innerHTML = '';
  if (cart.items.length === 0) {
    cartItemsContainer.innerHTML = `
      <div style="text-align: center; padding: 4rem 1rem; color: #64748b;">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="1.5" style="margin-bottom: 1rem;">
          <path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"></path>
          <line x1="3" y1="6" x2="21" y2="6"></line>
        </svg>
        <p style="font-size: 1.05rem; font-weight: 600; color: #1e293b; margin-bottom: 0.25rem;">Your bag is empty</p>
        <p style="font-size: 0.88rem;">Explore our collections to add footwear.</p>
      </div>
    `;
    document.getElementById('btn-checkout').disabled = true;
    return;
  }
  
  document.getElementById('btn-checkout').disabled = false;
  
  cart.items.forEach(item => {
    // Find item image from local cache or fallback
    const matchedProduct = localProducts.find(p => p.id === item.id);
    const itemImg = matchedProduct ? matchedProduct.image : 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=200&auto=format&fit=crop&q=80';

    const div = document.createElement('div');
    div.className = 'cart-item';
    div.innerHTML = `
      <img src="${itemImg}" alt="${item.name}" class="cart-item-thumb" onerror="this.src='https://placehold.co/100x100/1e293b/ffffff?text=Shoe'">
      <div class="cart-item-info">
        <div class="cart-item-title">${item.name}</div>
        <div class="cart-item-price">₹${item.price.toLocaleString('en-IN')} &nbsp;•&nbsp; Size ${item.size || '8'}</div>
      </div>
      <div class="cart-item-controls">
        <button class="qty-btn dec-qty" data-id="${item.id}" data-qty="${item.quantity - 1}" title="Decrease quantity">−</button>
        <span>${item.quantity}</span>
        <button class="qty-btn inc-qty" data-id="${item.id}" data-qty="${item.quantity + 1}" title="Increase quantity">+</button>
      </div>
    `;
    cartItemsContainer.appendChild(div);
  });

  // Attach events for +/-
  document.querySelectorAll('.qty-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const id = e.currentTarget.getAttribute('data-id');
      const newQty = parseInt(e.currentTarget.getAttribute('data-qty'), 10);
      updateCartAPI(id, newQty);
    });
  });
}

const getCartQty = (id) => {
  const btn = document.querySelector(`.inc-qty[data-id="${id}"]`);
  return btn ? parseInt(btn.getAttribute('data-qty'), 10) - 1 : 0;
};

// --- Gateway Pipeline Authority & Execution Management ---
const sleep = (ms) => new Promise(res => setTimeout(res, ms));

function resetPipelineModal() {
  const modal = document.getElementById('pipeline-modal');
  const stepsList = document.getElementById('pipeline-steps-list');
  const decisionBox = document.getElementById('pipeline-decision-box');
  const actionsBox = document.getElementById('pipeline-actions');
  const canonicalDetails = document.getElementById('canonical-details');
  const canonicalJson = document.getElementById('canonical-json-content');
  const protocolPill = document.getElementById('pipeline-protocol-pill');

  if (protocolPill) protocolPill.textContent = 'ACP 2026';
  if (decisionBox) {
    decisionBox.className = 'pipeline-decision-box hidden';
    decisionBox.innerHTML = '';
  }
  if (actionsBox) actionsBox.classList.add('hidden');
  if (canonicalDetails) canonicalDetails.classList.add('hidden');
  if (canonicalJson) canonicalJson.textContent = 'Connecting to Gateway... Canonical normalization in progress.';

  if (stepsList) {
    stepsList.innerHTML = `
      <li class="pipeline-step-item active" id="step-init">
        <div class="step-icon">⚡</div>
        <div class="step-info">
          <div class="step-title">Connecting to Agent Gateway</div>
          <div class="step-desc">Transmitting ACP protocol envelope to Gateway authority...</div>
        </div>
      </li>
    `;
  }
  if (modal) modal.classList.add('active');
}

function closePipelineModal() {
  const modal = document.getElementById('pipeline-modal');
  if (modal) modal.classList.remove('active');
}

function renderCanonicalRequest(canonicalData) {
  const canonicalJson = document.getElementById('canonical-json-content');
  if (canonicalJson && canonicalData) {
    canonicalJson.textContent = JSON.stringify(canonicalData, null, 2);
  }
}

async function renderRealPipelineStages(stages, protocol, decision, reason, razorpayOrderId) {
  const stepsList = document.getElementById('pipeline-steps-list');
  const protocolPill = document.getElementById('pipeline-protocol-pill');
  if (protocolPill && protocol) {
    protocolPill.textContent = protocol.toUpperCase() + ' (2026)';
  }

  if (!stepsList) return;
  stepsList.innerHTML = '';

  for (let i = 0; i < stages.length; i++) {
    const stage = stages[i];
    const li = document.createElement('li');
    li.className = 'pipeline-step-item active';
    li.id = `stage-${stage.stage.toLowerCase()}`;

    let iconSymbol = '●';
    let statusClass = 'active';

    if (stage.status === 'PASSED') {
      iconSymbol = '✓';
      statusClass = 'completed';
    } else if (stage.status === 'FAILED' || stage.status === 'BLOCK') {
      iconSymbol = '✕';
      statusClass = 'failed';
    } else if (stage.status === 'REVIEW') {
      iconSymbol = '⚠';
      statusClass = 'review';
    } else if (stage.status === 'ALLOW') {
      iconSymbol = '★';
      statusClass = 'completed';
    }

    li.innerHTML = `
      <div class="step-icon">${iconSymbol}</div>
      <div class="step-info">
        <div class="step-title">${stage.name || stage.stage}</div>
        <div class="step-desc">${stage.details || stage.reason || ''}</div>
      </div>
    `;

    stepsList.appendChild(li);
    await sleep(220); // Smooth real-time progression

    li.className = `pipeline-step-item ${statusClass}`;
  }

  // Render Decision Box
  const decisionBox = document.getElementById('pipeline-decision-box');
  const actionsBox = document.getElementById('pipeline-actions');

  if (decisionBox) {
    decisionBox.classList.remove('hidden');
    if (decision === 'ALLOW') {
      decisionBox.className = 'pipeline-decision-box decision-allow';
      decisionBox.innerHTML = `
        <div class="decision-status">⚡ GATEWAY DECISION: ALLOW</div>
        <div class="decision-reason">${reason || 'All 5 security gates passed & authorized by Gateway.'}</div>
        ${razorpayOrderId ? `<div class="order-info">Razorpay Test Mode Order: <code>${razorpayOrderId}</code></div>` : ''}
      `;
    } else if (decision === 'REVIEW') {
      decisionBox.className = 'pipeline-decision-box decision-review';
      decisionBox.innerHTML = `
        <div class="decision-status">⚠️ GATEWAY DECISION: REVIEW REQUIRED</div>
        <div class="decision-reason">${reason || 'Transaction flagged for manual merchant review. Razorpay order NOT created.'}</div>
      `;
      if (actionsBox) actionsBox.classList.remove('hidden');
    } else {
      decisionBox.className = 'pipeline-decision-box decision-block';
      decisionBox.innerHTML = `
        <div class="decision-status">🛑 GATEWAY DECISION: BLOCKED</div>
        <div class="decision-reason">${reason || 'Transaction rejected by Gateway policy. Razorpay order was NOT created.'}</div>
      `;
      if (actionsBox) actionsBox.classList.remove('hidden');
    }
  }
}

// Modal Toggle Listeners
document.addEventListener('DOMContentLoaded', () => {

  const toggleCanonicalBtn = document.getElementById('btn-toggle-canonical');
  const canonicalDetails = document.getElementById('canonical-details');
  const canonicalChevron = document.getElementById('canonical-chevron');
  if (toggleCanonicalBtn && canonicalDetails) {
    toggleCanonicalBtn.addEventListener('click', () => {
      const isHidden = canonicalDetails.classList.contains('hidden');
      if (isHidden) {
        canonicalDetails.classList.remove('hidden');
        if (canonicalChevron) canonicalChevron.textContent = '▲';
      } else {
        canonicalDetails.classList.add('hidden');
        if (canonicalChevron) canonicalChevron.textContent = '▼';
      }
    });
  }

  const dismissPipelineBtn = document.getElementById('btn-dismiss-pipeline');
  if (dismissPipelineBtn) {
    dismissPipelineBtn.addEventListener('click', () => {
      closePipelineModal();
      openCart();
    });
  }
});

// --- Checkout Intent & Razorpay Modal ---
async function checkoutIntent() {
  const btn = document.getElementById('btn-checkout');
  btn.disabled = true;
  btn.innerHTML = '<span>Contacting Gateway...</span> <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 14 14"></polyline></svg>';
  
  checkoutMessage.style.display = 'block';
  checkoutMessage.textContent = 'Initiating Agent Commerce Gateway security checks...';
  checkoutMessage.className = 'info-msg';

  // 1. Close cart drawer and open real Gateway Pipeline Modal
  closeCart();
  resetPipelineModal();

  try {
    // 2. Transmit request to Storefront backend -> Gateway /api/v1/execute
    const res = await fetch('/api/checkout-intent', { method: 'POST' });
    const result = await res.json();
    const data = result.data || {};

    // 3. Render normalized canonical request inspector
    if (data.canonical_request) {
      renderCanonicalRequest(data.canonical_request);
    }

    // 4. Render real backend pipeline stages
    const stages = data.pipeline_stages || [
      { stage: "REQUEST", name: "Incoming Protocol Request", status: "PASSED", details: "Protocol payload received" },
      { stage: "PROTOCOL_ADAPTER", name: "Canonical Normalization", status: "PASSED", details: "Converted to canonical structure" },
      { stage: "POLICY", name: "Merchant Policy Check", status: result.success ? "PASSED" : "FAILED", details: result.details || "Policy evaluation" },
      { stage: "DECISION", name: "Gateway Decision", status: data.gateway_decision || (result.success ? "ALLOW" : "BLOCK"), reason: result.details }
    ];

    await renderRealPipelineStages(
      stages,
      data.protocol || 'acp',
      data.gateway_decision || (result.success ? 'ALLOW' : 'BLOCK'),
      data.reason || result.details,
      data.razorpay_order_id
    );

    if (res.ok && result.success && data.gateway_decision === 'ALLOW') {
      // Clear cart
      renderCart({ items: [], total_amount: 0 });

      const paidAmountFormatted = `₹${(data.total_amount || (data.amount_minor / 100)).toLocaleString('en-IN')}`;
      checkoutMessage.innerHTML = `⚡ Gateway Decision: <strong>ALLOW</strong><br><small>Razorpay Order: <code>${data.razorpay_order_id}</code></small>`;
      checkoutMessage.className = 'success-msg';

      await sleep(600); // Brief moment for user to appreciate the all-green ALLOW gates

      // Launch Razorpay Checkout Modal
      if (typeof Razorpay !== 'undefined' && data.razorpay_key_id && data.razorpay_order_id) {
        const options = {
          key: data.razorpay_key_id,
          amount: data.amount_minor,
          currency: data.currency || 'INR',
          name: 'Apex Footwear',
          description: 'Agent Commerce Gateway Checkout',
          order_id: data.razorpay_order_id,
          handler: async function (response) {
            closePipelineModal();
            checkoutMessage.innerHTML = 'Verifying HMAC signature with Gateway...';
            checkoutMessage.className = 'info-msg';

            try {
              const verifyRes = await fetch('/api/verify-payment', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  razorpay_order_id: response.razorpay_order_id,
                  razorpay_payment_id: response.razorpay_payment_id,
                  razorpay_signature: response.razorpay_signature,
                  amount_minor: data.amount_minor,
                  currency: data.currency
                })
              });
              const verifyData = await verifyRes.json();

              if (verifyRes.ok && verifyData.success) {
                checkoutMessage.innerHTML = `
                  <div style="font-size: 1rem; font-weight: 800; color: #15803d; margin-bottom: 0.35rem;">
                    ✅ Payment Captured & Verified!
                  </div>
                  <div style="font-size: 0.8rem; color: #166534; line-height: 1.4;">
                    Payment ID: <code>${response.razorpay_payment_id}</code><br>
                    Order ID: <code>${response.razorpay_order_id}</code><br>
                    Audit Event committed to Gateway Ledger.
                  </div>
                `;
                checkoutMessage.className = 'success-msg';

                // Remove custom footstore receipt modal as requested by user.
                // Just return to the catalog after successful Razorpay verification.
                setTimeout(() => {
                  closePipelineModal();
                  showCatalog();
                  document.getElementById('checkout-message').style.display = 'none';
                }, 3000);
              } else {
                checkoutMessage.innerHTML = `⚠️ Payment captured check failed: ${verifyData.error || 'Signature check failed'}`;
                checkoutMessage.className = 'error-msg';
                alert('Payment captured, but verification failed: ' + (verifyData.error || 'Signature mismatch'));
              }
            } catch (err) {
              checkoutMessage.innerHTML = `Verification error: ${err.message}`;
              checkoutMessage.className = 'error-msg';
            }
          },
          modal: {
            ondismiss: function () {
              checkoutMessage.innerHTML = `⚡ Order <code>${data.razorpay_order_id}</code> created via Gateway. Payment pending.`;
              checkoutMessage.className = 'info-msg';
            }
          }
        };

        const rzp = new Razorpay(options);
        rzp.open();
      }
    } else {
      // BLOCKED or REVIEW: Razorpay is NEVER opened
      const errMsg = result.details || result.error || 'Checkout intent blocked by Gateway';
      if (errMsg.includes('/pay')) {
        checkoutMessage.innerHTML = `${errMsg}<br><a href="/pay" target="_blank" style="color:#2563eb;font-weight:700;text-decoration:underline;margin-top:0.25rem;display:inline-block;">Open Store Setup (/pay) ↗</a>`;
      } else {
        checkoutMessage.textContent = errMsg;
      }
      checkoutMessage.className = 'error-msg';
    }
  } catch (err) {
    const actionsBox = document.getElementById('pipeline-actions');
    if (actionsBox) actionsBox.classList.remove('hidden');
    checkoutMessage.textContent = 'Network error during checkout intent: ' + err.message;
    checkoutMessage.className = 'error-msg';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>Proceed to Checkout</span> <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>';
  }
}


// --- View Toggles ---
function showCatalog() {
  viewProduct.classList.add('hidden');
  viewCatalog.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showProductDetail() {
  viewCatalog.classList.add('hidden');
  viewProduct.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function toggleCart() {
  const isHidden = cartSidebar.classList.contains('hidden');
  if (isHidden) {
    openCart();
  } else {
    closeCart();
  }
}

function openCart() {
  cartSidebar.classList.remove('hidden');
  cartOverlay.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeCart() {
  cartSidebar.classList.add('hidden');
  cartOverlay.classList.add('hidden');
  document.body.style.overflow = '';
}


