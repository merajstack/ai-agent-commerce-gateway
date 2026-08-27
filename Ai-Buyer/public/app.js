/**
 * AI Buyer — Minimalist Controller with Visible Protocol Normalization & 3-Panel Inspector
 * Supports: ACP (RFC 2026-01-16) and x402 v2 (HTTP 402 / Base Sepolia)
 */

let chatHistory = [];
let activeProtocol = 'acp';
let latestProtocolPayload = null;
let isProcessing = false;
let currentCheckoutData = null;

// Protocol Metadata & Visual Identities
const PROTOCOL_META = {
  acp: {
    id: 'acp',
    name: 'ACP (RFC 2026-01-16)',
    badge: 'ACP 2026',
    icon: '⚡',
    adapter: 'ACPAdapter',
    colorClass: 'theme-acp',
    badgeClass: 'protocol-acp',
    caption: 'Active Protocol: <strong>ACP 2026</strong> — Authoritative Agentic Commerce Protocol (RFC 2026-01-16)',
    desc: 'Opaque Bearer Token + Idempotency-Key Header Normalized via ACPAdapter'
  },
  x402: {
    id: 'x402',
    name: 'x402 v2 (HTTP 402 / Base Sepolia)',
    badge: 'x402 v2',
    icon: '🔗',
    adapter: 'X402Adapter',
    colorClass: 'theme-x402',
    badgeClass: 'protocol-x402',
    caption: 'Active Protocol: <strong>x402 v2</strong> — Authoritative PaymentPayload on Base Sepolia (eip155:84532)',
    desc: 'PaymentPayload with EIP-712/EIP-3009 scheme-specific proof Normalized via X402Adapter'
  }
};

// Initialize App
document.addEventListener('DOMContentLoaded', async () => {
  await checkStatus();
  await refreshCart();
  setInterval(refreshCart, 4000);

  const inputEl = document.getElementById('user-input');
  if (inputEl) inputEl.focus();

  // Esc key closes modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeNormalizationModal();
    }
  });
});

// Check Server Status
async function checkStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();

    if (data.cart) {
      renderCartState(data.cart);
    }
  } catch (err) {
    console.error('Status check failed:', err);
  }
}

// Toggle Left Sidebar
function toggleLeftSidebar() {
  const sidebar = document.getElementById('sidebar-left');
  if (sidebar) sidebar.classList.toggle('collapsed');
}

// Toggle Right Inspector Drawer
function toggleInspector(tabId) {
  const drawer = document.getElementById('inspector-drawer');
  if (!drawer) return;

  if (tabId) {
    drawer.classList.remove('collapsed');
    switchDrawerTab(tabId);
  } else {
    drawer.classList.toggle('collapsed');
  }
}

// Switch Drawer Tab
function switchDrawerTab(tabId) {
  document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.drawer-pane').forEach(p => p.classList.remove('active'));

  const btn = document.getElementById(`tab-btn-${tabId}`);
  const pane = document.getElementById(`pane-${tabId}`);
  if (btn) btn.classList.add('active');
  if (pane) pane.classList.add('active');
}

// Protocol Dropdown Handler
function onProtocolChange(e) {
  activeProtocol = e.target.value;
  const meta = PROTOCOL_META[activeProtocol] || PROTOCOL_META.acp;

  const captionEl = document.getElementById('active-protocol-caption');
  if (captionEl) captionEl.innerHTML = meta.caption;

  const navBadge = document.getElementById('nav-protocol-badge');
  if (navBadge) navBadge.textContent = meta.badge;

  const navIcon = document.getElementById('nav-protocol-icon');
  if (navIcon) navIcon.textContent = meta.icon;

  const drawerBadge = document.getElementById('drawer-protocol-badge');
  if (drawerBadge) drawerBadge.textContent = meta.badge;

  const drawerTag = document.getElementById('drawer-code-tag');
  if (drawerTag) drawerTag.textContent = activeProtocol === 'x402' ? 'x402 v2 PaymentPayload' : 'RAW RFC JSON';

  const cartBtn = document.getElementById('btn-cart-checkout-action');
  if (cartBtn) cartBtn.textContent = `${meta.icon} Checkout with ${meta.badge}`;
}

// Quick Prompt Click Handler
function sendPrompt(promptText) {
  const inputEl = document.getElementById('user-input');
  if (inputEl) {
    inputEl.value = promptText;
    document.getElementById('chat-form').dispatchEvent(new Event('submit', { cancelable: true }));
  }
}

// New Chat Reset
function startNewChat() {
  chatHistory = [];
  currentCheckoutData = null;
  const messageThread = document.getElementById('message-thread');
  if (messageThread) messageThread.innerHTML = '';

  const welcomeHero = document.getElementById('welcome-hero');
  if (welcomeHero) welcomeHero.classList.remove('hidden');

  const inputEl = document.getElementById('user-input');
  if (inputEl) {
    inputEl.value = '';
    inputEl.focus();
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// 3-PANEL NORMALIZATION INSPECTOR MODAL CONTROLLER
// ══════════════════════════════════════════════════════════════════════════════

window.openNormalizationModal = function() {
  const modal = document.getElementById('normalization-modal');
  if (modal) {
    modal.classList.add('active');
    modal.setAttribute('aria-hidden', 'false');
  }
};

window.closeNormalizationModal = function() {
  const modal = document.getElementById('normalization-modal');
  if (modal) {
    modal.classList.remove('active');
    modal.setAttribute('aria-hidden', 'true');
  }
};

function renderThreePanelInspector(protocol, rawPayload, gatewayData) {
  currentCheckoutData = { protocol, rawPayload, gatewayData };
  const meta = PROTOCOL_META[protocol] || PROTOCOL_META.acp;

  // 1. Modal Top Bar
  const iconEl = document.getElementById('modal-protocol-icon');
  if (iconEl) iconEl.textContent = meta.icon;

  const protoBadge = document.getElementById('modal-protocol-badge');
  if (protoBadge) {
    protoBadge.textContent = meta.badge;
    protoBadge.className = `protocol-identity-pill ${meta.badgeClass}`;
  }

  const adapterBadge = document.getElementById('modal-adapter-badge');
  const adapterName = gatewayData?.adapter_used || (protocol === 'x402' ? 'X402Adapter' : 'ACPAdapter');
  if (adapterBadge) adapterBadge.textContent = adapterName;

  const decisionBadge = document.getElementById('modal-decision-badge');
  const decision = gatewayData?.gateway_decision || gatewayData?.final_decision || 'ALLOW';
  if (decisionBadge) {
    decisionBadge.textContent = decision;
    decisionBadge.className = `decision-pill ${decision.toLowerCase()}`;
  }

  const subtitleEl = document.getElementById('modal-protocol-subtitle');
  if (subtitleEl) {
    subtitleEl.innerHTML = `Proving <strong>${meta.name}</strong> payload maps deterministically into unified canonical <code>CommerceRequest</code> via <code>${adapterName}</code>`;
  }

  const timeVal = document.getElementById('modal-timestamp-val');
  if (timeVal) {
    timeVal.textContent = gatewayData?.timestamp ? new Date(gatewayData.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString();
  }

  // 2. PANEL 1: Raw Protocol
  const rawCol = document.getElementById('panel-raw-col');
  if (rawCol) {
    rawCol.className = `panel-column panel-raw ${meta.colorClass}`;
  }

  const rawDesc = document.getElementById('panel-raw-desc');
  if (rawDesc) {
    rawDesc.innerHTML = protocol === 'x402'
      ? 'Exact <strong>x402 v2 PaymentPayload</strong> schema on Base Sepolia (eip155:84532).'
      : 'Exact <strong>ACP (RFC 2026-01-16)</strong> checkout request with line items & bearer token.';
  }

  const rawJsonEl = document.getElementById('modal-raw-json');
  if (rawJsonEl && rawPayload) {
    rawJsonEl.innerHTML = `<code>${JSON.stringify(rawPayload, null, 2)}</code>`;
  }

  // Field Chips for Panel 1
  const chipsContainer = document.getElementById('raw-field-chips');
  if (chipsContainer && rawPayload) {
    let chipsHtml = '';
    if (protocol === 'acp') {
      const buyerId = rawPayload.buyer?.agent_id || 'unknown';
      const idemp = rawPayload.idempotency_key || 'nonce';
      const itemCount = rawPayload.items?.length || 0;
      chipsHtml = `
        <span class="field-chip acp-chip">buyer.agent_id: "${buyerId}"</span>
        <span class="field-chip acp-chip">idempotency_key: "${idemp.slice(0, 14)}..."</span>
        <span class="field-chip acp-chip">api_version: "${rawPayload.api_version || '2026-01-16'}"</span>
        <span class="field-chip acp-chip">items.length: ${itemCount}</span>
        <span class="field-chip acp-chip">bearer_token: [PROTECTED]</span>
      `;
    } else {
      const scheme = rawPayload.accepted?.scheme || 'exact';
      const network = rawPayload.accepted?.network || 'eip155:84532';
      const atomicAmount = rawPayload.accepted?.amount || '0';
      const v2 = rawPayload.x402Version || 2;
      chipsHtml = `
        <span class="field-chip x402-chip">x402Version: ${v2}</span>
        <span class="field-chip x402-chip">accepted.scheme: "${scheme}"</span>
        <span class="field-chip x402-chip">accepted.network: "${network}"</span>
        <span class="field-chip x402-chip">accepted.amount: "${atomicAmount}"</span>
        <span class="field-chip x402-chip">payload.signature: [0x... SIGNED]</span>
      `;
    }
    chipsContainer.innerHTML = chipsHtml;
  }

  // 3. PANEL 2: Canonical CommerceRequest
  const canonicalJsonEl = document.getElementById('modal-canonical-json');
  const canonicalData = gatewayData?.canonical_request;
  if (canonicalJsonEl && canonicalData) {
    canonicalJsonEl.innerHTML = `<code>${JSON.stringify(canonicalData, null, 2)}</code>`;
  }

  // Mapping Rows for Panel 2
  const mappingRowsEl = document.getElementById('modal-mapping-rows');
  if (mappingRowsEl && canonicalData) {
    let mappingHtml = '';
    const totalMinor = canonicalData.total?.amount_minor || 0;
    const totalMajor = (totalMinor / 100).toLocaleString('en-IN');
    const currency = canonicalData.total?.currency || 'INR';
    const buyerId = canonicalData.buyer_agent_id || 'unknown';
    const merchantId = canonicalData.merchant_id || 'unknown';
    const txId = canonicalData.transaction_id || 'unknown';

    if (protocol === 'acp') {
      mappingHtml = `
        <div class="mapping-row">
          <span class="mapping-source">items[].unit_amount</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">total.amount_minor</span>
          <span class="mapping-val">${totalMinor} (${currency} ${totalMajor})</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">buyer.agent_id</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">buyer_agent_id</span>
          <span class="mapping-val">"${buyerId}"</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">merchant_id</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">merchant_id</span>
          <span class="mapping-val">"${merchantId}"</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">idempotency_key</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">nonce / transaction_id</span>
          <span class="mapping-val">"${txId.slice(0, 18)}..."</span>
        </div>
      `;
    } else {
      mappingHtml = `
        <div class="mapping-row">
          <span class="mapping-source">accepted.amount</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">total.amount_minor</span>
          <span class="mapping-val">${totalMinor} (${currency} ${totalMajor})</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">accepted.extra.buyer_agent_id</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">buyer_agent_id</span>
          <span class="mapping-val">"${buyerId}"</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">accepted.extra.merchant_id</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">merchant_id</span>
          <span class="mapping-val">"${merchantId}"</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">payload.authorization.nonce</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">nonce / transaction_id</span>
          <span class="mapping-val">"${txId.slice(0, 18)}..."</span>
        </div>
        <div class="mapping-row">
          <span class="mapping-source">accepted.network</span>
          <span class="mapping-arrow">→</span>
          <span class="mapping-target">verified_context</span>
          <span class="mapping-val">"eip155:84532"</span>
        </div>
      `;
    }
    mappingRowsEl.innerHTML = mappingHtml;
  }

  // 4. PANEL 3: Security Pipeline Stages
  const stagesListEl = document.getElementById('modal-pipeline-stages-list');
  const stages = gatewayData?.pipeline_stages || [];
  if (stagesListEl && stages.length > 0) {
    let stagesHtml = '';
    stages.forEach(stage => {
      const isPassed = stage.status === 'PASSED' || stage.status === 'ALLOW';
      const isFailed = stage.status === 'FAILED' || stage.status === 'BLOCK';
      const isReview = stage.status === 'REVIEW';
      const stageClass = isPassed ? 'passed' : (isFailed ? 'failed' : (isReview ? 'review' : 'inactive'));
      const icon = isPassed ? '✓' : (isFailed ? '✕' : (isReview ? '⚠' : '●'));

      stagesHtml += `
        <div class="modal-stage-item ${stageClass}">
          <div class="modal-stage-icon">${icon}</div>
          <div class="modal-stage-content">
            <div class="modal-stage-title">${stage.name || stage.stage}</div>
            <div class="modal-stage-desc">${stage.details || stage.reason || stage.status}</div>
          </div>
        </div>
      `;
    });
    stagesListEl.innerHTML = stagesHtml;
  }

  // Panel 3 Decision Banner
  const bannerEl = document.getElementById('modal-decision-banner');
  const bannerStatus = document.getElementById('modal-decision-banner-status');
  const bannerReason = document.getElementById('modal-decision-banner-reason');
  const orderRow = document.getElementById('modal-order-handoff-row');
  const orderIdEl = document.getElementById('modal-razorpay-order-id');
  const payBtnWrap = document.getElementById('modal-pay-button-wrap');

  if (bannerEl) {
    bannerEl.className = `modal-decision-banner ${decision === 'ALLOW' ? 'decision-allow' : 'decision-block'}`;
  }
  if (bannerStatus) {
    bannerStatus.textContent = decision === 'ALLOW' ? '⚡ GATEWAY DECISION: ALLOW' : '🛑 GATEWAY DECISION: BLOCKED';
  }
  if (bannerReason) {
    bannerReason.textContent = gatewayData?.reason || (decision === 'ALLOW' ? 'All security checks passed & verified by Agent Commerce Gateway.' : 'Transaction rejected by Gateway security policy.');
  }

  if (decision === 'ALLOW' && gatewayData?.razorpay_order_id) {
    if (orderRow) orderRow.classList.remove('hidden');
    if (orderIdEl) orderIdEl.textContent = gatewayData.razorpay_order_id;
    if (payBtnWrap) {
      const amountMajor = gatewayData.amount_minor ? (gatewayData.amount_minor / 100).toLocaleString('en-IN') : '0';
      payBtnWrap.innerHTML = `
        <button 
          class="btn-pay-now" 
          style="padding: 6px 14px; font-size: 12px;"
          onclick="launchRazorpayCheckout('${gatewayData.razorpay_order_id}', '${gatewayData.razorpay_key_id || 'rzp_test_TSuG9gfvyjCsK2'}', ${gatewayData.amount_minor || 0}, '${gatewayData.currency || 'INR'}')"
        >
          💳 Pay ₹${amountMajor}
        </button>
      `;
    }
  } else if (orderRow) {
    orderRow.classList.add('hidden');
  }
}

window.copyRawProtocolJson = function() {
  if (currentCheckoutData && currentCheckoutData.rawPayload) {
    navigator.clipboard.writeText(JSON.stringify(currentCheckoutData.rawPayload, null, 2));
    alert('Raw protocol JSON copied to clipboard!');
  }
};

window.copyCanonicalJson = function() {
  if (currentCheckoutData && currentCheckoutData.gatewayData && currentCheckoutData.gatewayData.canonical_request) {
    navigator.clipboard.writeText(JSON.stringify(currentCheckoutData.gatewayData.canonical_request, null, 2));
    alert('Canonical CommerceRequest JSON copied to clipboard!');
  }
};

// ══════════════════════════════════════════════════════════════════════════════
// CHAT & SUBMISSION PIPELINE
// ══════════════════════════════════════════════════════════════════════════════

async function handleChatSubmit(e) {
  e.preventDefault();
  if (isProcessing) return;

  const inputEl = document.getElementById('user-input');
  const userText = inputEl.value.trim();
  if (!userText) return;

  const welcomeHero = document.getElementById('welcome-hero');
  if (welcomeHero) welcomeHero.classList.add('hidden');

  appendMessage('user', userText, activeProtocol);
  inputEl.value = '';

  isProcessing = true;
  const sendBtn = document.getElementById('send-btn');
  if (sendBtn) sendBtn.disabled = true;

  const typingId = showTypingIndicator();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: userText,
        history: chatHistory,
        protocol: activeProtocol
      })
    });

    removeTypingIndicator(typingId);

    const result = await res.json();
    if (!result.success) {
      appendMessage('assistant', `⚠️ **Error**: ${result.error || result.details || 'Processing failed'}`);
      return;
    }

    const data = result.data;
    chatHistory.push({ role: 'user', content: userText });
    chatHistory.push({ role: 'assistant', content: data.message });

    appendAssistantMessage(data);

    // Update Drawer & Modal Data
    const rawReq = data.rawProtocolRequest || data.gatewayResponse?.raw_payload;
    if (rawReq) {
      renderProtocolPayload(rawReq, data.protocolMetadata);
    }

    if (data.gatewayResponse) {
      currentCheckoutData = {
        protocol: activeProtocol,
        rawPayload: rawReq,
        gatewayData: data.gatewayResponse
      };
      renderGatewayResults(data.gatewayResponse);
      renderThreePanelInspector(activeProtocol, rawReq, data.gatewayResponse);

      // Open inspector drawer to show proof
      const drawer = document.getElementById('inspector-drawer');
      if (drawer && drawer.classList.contains('collapsed')) {
        drawer.classList.remove('collapsed');
      }
      switchDrawerTab('gateway');
    }

    if (data.cart) {
      renderCartState(data.cart);
    }
  } catch (err) {
    removeTypingIndicator(typingId);
    appendMessage('assistant', `⚠️ **Connection Error**: ${err.message}`);
  } finally {
    isProcessing = false;
    if (sendBtn) sendBtn.disabled = false;
    inputEl.focus();
  }
}

// Append Simple User Message
function appendMessage(role, text, protocol = null) {
  const container = document.getElementById('message-thread');
  const item = document.createElement('div');
  item.className = `message-item ${role}`;

  const protocolTag = protocol ? `<div class="protocol-tag-pill ${protocol === 'x402' ? 'protocol-x402' : 'protocol-acp'}">${protocol.toUpperCase()}</div>` : '';

  if (role === 'user') {
    item.innerHTML = `
      <div class="message-box">
        ${protocolTag}
        <div class="message-body">${formatMarkdown(text)}</div>
      </div>
    `;
  } else {
    item.innerHTML = `
      <div class="message-avatar">🤖</div>
      <div class="message-box">
        <div class="message-body">${formatMarkdown(text)}</div>
      </div>
    `;
  }

  container.appendChild(item);
  scrollToBottom();
}

// Append Assistant Message with Tool Badges, 3-Panel Inspector Button, & Pay Button
function appendAssistantMessage(data) {
  const container = document.getElementById('message-thread');
  const item = document.createElement('div');
  item.className = 'message-item assistant';

  let toolPillsHtml = '';
  if (data.activityLogs && data.activityLogs.length > 0) {
    toolPillsHtml = `<div class="tool-pills-row">`;
    data.activityLogs.forEach(log => {
      if (log.status === 'running') return;
      const icon = log.status === 'success' ? '✓' : 'ℹ️';
      const chipClass = log.status === 'success' ? 'success' : 'failed';
      const label = log.tool === 'checkout' ? (activeProtocol === 'x402' ? `🔗 x402 v2 PaymentPayload` : `⚡ ACP 2026 Protocol Request`) :
                    log.tool === 'add_to_cart' ? `🛒 Added to Cart` :
                    log.tool === 'search_products' ? `🔍 Catalog Search` : log.tool;
      toolPillsHtml += `<span class="tool-badge ${chipClass}">${icon} ${label}</span>`;
    });
    toolPillsHtml += `</div>`;
  }

  // Check if Gateway returned response
  let payCardHtml = '';
  let inspectBtnHtml = '';
  const gw = data.gatewayResponse;

  if (gw) {
    const isAllow = gw.gateway_decision === 'ALLOW' || gw.final_decision === 'ALLOW';
    const amountMajor = gw.amount_minor ? (gw.amount_minor / 100).toLocaleString('en-IN') : (gw.total_amount || 0).toLocaleString('en-IN');
    const orderId = gw.razorpay_order_id;
    const keyId = gw.razorpay_key_id || 'rzp_test_TSuG9gfvyjCsK2';
    const amountMinor = gw.amount_minor || 0;
    const currency = gw.currency || 'INR';

    inspectBtnHtml = `
      <div style="margin-top: 10px;">
        <button class="btn-inspect-stream" onclick="openNormalizationModal()">
          <span>🔍 View Live 3-Panel Normalization Proof</span>
        </button>
      </div>
    `;

    if (isAllow && orderId) {
      payCardHtml = `
        <div class="payment-action-card" id="pay-card-${orderId}">
          <div class="pay-card-header">
            <span class="pay-label">Checkout Ready</span>
            <span class="pay-amount-display">₹${amountMajor}</span>
          </div>
          <button 
            class="btn-pay-now" 
            onclick="launchRazorpayCheckout('${orderId}', '${keyId}', ${amountMinor}, '${currency}')"
          >
            💳 Pay ₹${amountMajor} (Razorpay Sandbox)
          </button>
          <div class="pay-security-note">
            🔒 Authorized via Agent Commerce Gateway &amp; Razorpay Sandbox
          </div>
        </div>
      `;
    }
  }

  item.innerHTML = `
    <div class="message-avatar">🤖</div>
    <div class="message-box">
      ${toolPillsHtml}
      <div class="message-body">${formatMarkdown(data.message)}</div>
      ${inspectBtnHtml}
      ${payCardHtml}
    </div>
  `;

  container.appendChild(item);
  scrollToBottom();
}

// Launch Razorpay Standard Checkout Modal
window.launchRazorpayCheckout = function(orderId, keyId, amountMinor, currency) {
  if (typeof Razorpay === 'undefined') {
    alert('Razorpay Checkout SDK is loading, please try again in a moment.');
    return;
  }

  const effectiveKey = keyId || 'rzp_test_TSuG9gfvyjCsK2';
  const effectiveAmount = parseInt(amountMinor, 10) || 10000;
  const effectiveCurrency = currency || 'INR';

  const options = {
    key: effectiveKey,
    amount: effectiveAmount,
    currency: effectiveCurrency,
    name: 'Apex Footwear',
    description: 'AI Shopping Order (Agent Commerce Gateway)',
    handler: async function (response) {
      showPaymentVerifyingBubble();
      
      const payload = {
        razorpay_order_id: response.razorpay_order_id || orderId || `order_${Date.now()}`,
        razorpay_payment_id: response.razorpay_payment_id || `pay_${Date.now()}`,
        razorpay_signature: response.razorpay_signature || `sig_verified_${Date.now()}`,
        amount_minor: effectiveAmount,
        currency: effectiveCurrency
      };

      try {
        const verifyRes = await fetch('/api/verify-payment', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        const verifyResult = await verifyRes.json();
        removePaymentVerifyingBubble();

        if (verifyResult.success) {
          renderPaymentSuccessMessage(payload, verifyResult.data);
          await refreshCart();
        } else {
          appendMessage('assistant', `⚠️ **Payment Verification Failed**: ${verifyResult.error || 'Signature check failed'}`);
        }
      } catch (err) {
        removePaymentVerifyingBubble();
        appendMessage('assistant', `⚠️ **Payment Verification Network Error**: ${err.message}`);
      }
    },
    prefill: {
      name: 'Buyer Agent',
      email: 'buyer@agentcommerce.ai',
      contact: '9999999999'
    },
    theme: {
      color: '#6366f1'
    },
    modal: {
      ondismiss: function() {
        console.log('[Razorpay] Checkout modal dismissed');
      }
    }
  };

  // Only attach order_id if it is a real live server order id format (avoids SDK bad request crash)
  if (orderId && !orderId.startsWith('order_test_') && !orderId.startsWith('order_acp_') && !orderId.startsWith('order_x402_')) {
    options.order_id = orderId;
  }

  try {
    const rzp = new Razorpay(options);
    rzp.on('payment.failed', function (response) {
      removePaymentVerifyingBubble();
      appendMessage('assistant', `⚠️ **Razorpay Payment Failed**: ${response.error?.description || response.error?.reason || 'Transaction could not be processed'}`);
    });
    rzp.open();
  } catch (err) {
    console.error('Failed to open Razorpay modal:', err);
    alert('Error initializing Razorpay Checkout: ' + err.message);
  }
};

function showPaymentVerifyingBubble() {
  const container = document.getElementById('message-thread');
  const item = document.createElement('div');
  item.className = 'message-item assistant';
  item.id = 'verifying-payment-bubble';
  item.innerHTML = `
    <div class="message-avatar">🛡️</div>
    <div class="message-box">
      <div class="message-body" style="color: var(--accent-cyan);">
        <span>Verifying signature &amp; capturing payment via Gateway... ⏳</span>
      </div>
    </div>
  `;
  container.appendChild(item);
  scrollToBottom();
}

function removePaymentVerifyingBubble() {
  const el = document.getElementById('verifying-payment-bubble');
  if (el) el.remove();
}

function renderPaymentSuccessMessage(razorpayResponse, gatewayVerifyData) {
  const container = document.getElementById('message-thread');
  const item = document.createElement('div');
  item.className = 'message-item assistant';

  const payId = razorpayResponse.razorpay_payment_id;
  const orderId = razorpayResponse.razorpay_order_id;
  const status = gatewayVerifyData?.razorpay_payment_status || 'captured';

  item.innerHTML = `
    <div class="message-avatar">✅</div>
    <div class="message-box">
      <div class="payment-success-card">
        <div class="payment-success-title">
          <span>🎉 PAYMENT CAPTURED &amp; VERIFIED</span>
        </div>
        <div class="payment-meta-row">
          <span>Payment ID:</span>
          <code>${payId}</code>
        </div>
        <div class="payment-meta-row">
          <span>Order ID:</span>
          <code>${orderId}</code>
        </div>
        <div class="payment-meta-row">
          <span>Status:</span>
          <strong style="color: #34d399;">${status.toUpperCase()}</strong>
        </div>
        <div class="payment-meta-row">
          <span>Audit Ledger:</span>
          <span style="color: #38bdf8;">Committed to Gateway Audit Ledger</span>
        </div>
      </div>
    </div>
  `;

  container.appendChild(item);
  scrollToBottom();
}

// Format Markdown
function formatMarkdown(text) {
  if (!text) return '';
  return text
    .replace(/^### (.*$)/gim, '<h3 style="font-size:15px; font-weight:700; margin:6px 0;">$1</h3>')
    .replace(/^## (.*$)/gim, '<h2 style="font-size:16px; font-weight:700; margin:8px 0;">$1</h2>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n\n/g, '<p style="margin-bottom:8px;"></p>')
    .replace(/\n/g, '<br>')
    .replace(/> (.*$)/gim, '<blockquote>$1</blockquote>');
}

function showTypingIndicator() {
  const container = document.getElementById('message-thread');
  const id = `typing-${Date.now()}`;
  const item = document.createElement('div');
  item.className = 'message-item assistant';
  item.id = id;
  item.innerHTML = `
    <div class="message-avatar">🤖</div>
    <div class="message-box">
      <div class="message-body" style="color: var(--text-tertiary);">
        <span>Thinking &amp; consulting ${activeProtocol.toUpperCase()} protocol...</span>
      </div>
    </div>
  `;
  container.appendChild(item);
  scrollToBottom();
  return id;
}

function removeTypingIndicator(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function scrollToBottom() {
  const stream = document.getElementById('chat-stream');
  if (stream) {
    stream.scrollTop = stream.scrollHeight;
  }
}

// Render Protocol Payload in Right Inspector Drawer
function renderProtocolPayload(payload, metadata) {
  latestProtocolPayload = payload;
  const pre = document.getElementById('raw-protocol-json');
  if (pre) {
    pre.innerHTML = `<code>${JSON.stringify(payload, null, 2)}</code>`;
  }
}

// Copy Code Helper
window.copyProtocolJson = function() {
  if (!latestProtocolPayload) return;
  navigator.clipboard.writeText(JSON.stringify(latestProtocolPayload, null, 2));
  alert('Raw protocol request JSON copied to clipboard!');
};

// Render Gateway Stages & Canonical (Drawer)
function renderGatewayResults(gatewayData) {
  const badge = document.getElementById('gateway-decision-badge');
  const decision = gatewayData.gateway_decision || gatewayData.final_decision || 'ALLOW';
  
  if (badge) {
    badge.textContent = decision;
    badge.className = `decision-badge ${decision === 'ALLOW' ? 'allow' : 'block'}`;
  }

  // Canonical JSON
  const canonicalPre = document.getElementById('canonical-json-display');
  if (canonicalPre && gatewayData.canonical_request) {
    canonicalPre.innerHTML = `<code>${JSON.stringify(gatewayData.canonical_request, null, 2)}</code>`;
  }

  // Pipeline Stages
  const stagesList = document.getElementById('pipeline-stages-list');
  if (stagesList && gatewayData.pipeline_stages) {
    let stagesHtml = '';
    gatewayData.pipeline_stages.forEach((stage, idx) => {
      const isPassed = stage.status === 'PASSED' || stage.status === 'ALLOW';
      const isFailed = stage.status === 'FAILED' || stage.status === 'BLOCK';
      const isReview = stage.status === 'REVIEW';
      const stepClass = isPassed ? 'passed' : (isFailed ? 'failed' : (isReview ? 'review' : 'inactive'));
      const icon = isPassed ? '✓' : (isFailed ? '✕' : (isReview ? '⚠' : idx + 1));

      stagesHtml += `
        <div class="pipeline-step ${stepClass}">
          <div class="step-num">${icon}</div>
          <div class="step-info">
            <strong>${stage.name || stage.stage}</strong>
            <span>${stage.details || stage.reason || stage.status}</span>
          </div>
        </div>
      `;
    });
    stagesList.innerHTML = stagesHtml;
  }

  // Order Box
  const orderBox = document.getElementById('order-handoff-box');
  const orderIdEl = document.getElementById('handoff-order-id');
  const payBtnContainer = document.getElementById('inspector-pay-btn-container');

  if (orderBox && gatewayData.razorpay_order_id && decision === 'ALLOW') {
    orderBox.classList.remove('hidden');
    orderIdEl.textContent = gatewayData.razorpay_order_id;

    if (payBtnContainer) {
      const amountMajor = gatewayData.amount_minor ? (gatewayData.amount_minor / 100).toLocaleString('en-IN') : (gatewayData.total_amount || 0).toLocaleString('en-IN');
      payBtnContainer.innerHTML = `
        <button 
          class="btn-pay-now" 
          style="width: 100%; font-size: 13px; padding: 8px;"
          onclick="launchRazorpayCheckout('${gatewayData.razorpay_order_id}', '${gatewayData.razorpay_key_id || 'rzp_test_TSuG9gfvyjCsK2'}', ${gatewayData.amount_minor || 0}, '${gatewayData.currency || 'INR'}')"
        >
          💳 Pay ₹${amountMajor}
        </button>
      `;
    }
  } else if (orderBox) {
    orderBox.classList.add('hidden');
  }
}

// Toggle Canonical Expand in Drawer
function toggleCanonicalExpand() {
  const body = document.getElementById('canonical-accordion-content');
  const chevron = document.getElementById('canonical-chevron');
  if (body) {
    body.classList.toggle('hidden');
    chevron.textContent = body.classList.contains('hidden') ? '▼' : '▲';
  }
}

// Render Cart State
function renderCartState(cart) {
  const navCount = document.getElementById('nav-cart-count');
  const navTotal = document.getElementById('nav-cart-total');
  const subtotalVal = document.getElementById('cart-subtotal-val');
  const container = document.getElementById('cart-items-container');

  const items = cart.items || [];
  const total = cart.total_amount || 0;
  const count = items.reduce((sum, i) => sum + i.quantity, 0);

  if (navCount) navCount.textContent = `${count} ${count === 1 ? 'item' : 'items'}`;
  if (navTotal) navTotal.textContent = `₹${total.toLocaleString('en-IN')}`;
  if (subtotalVal) subtotalVal.textContent = `₹${total.toLocaleString('en-IN')}`;

  if (container) {
    if (items.length === 0) {
      container.innerHTML = `<div class="empty-cart-notice">Cart is currently empty.</div>`;
    } else {
      let html = '';
      items.forEach(item => {
        html += `
          <div class="cart-item-card">
            <div>
              <strong>${item.name}</strong>
              <div style="font-size: 11px; color: var(--text-tertiary);">Size: ${item.size || '9'} | Qty: ${item.quantity}</div>
            </div>
            <strong style="color: var(--accent-cyan);">₹${(item.price * item.quantity).toLocaleString('en-IN')}</strong>
          </div>
        `;
      });
      container.innerHTML = html;
    }
  }
}

// Refresh Cart from Storefront
async function refreshCart() {
  try {
    const res = await fetch('/api/cart');
    if (!res.ok) return;
    const data = await res.json();
    if (data.data) {
      renderCartState(data.data);
    }
  } catch (e) {
    // ignore
  }
}

// Clear Cart
async function clearCart() {
  try {
    const res = await fetch('/api/cart');
    const data = await res.json();
    if (data.data && data.data.items) {
      for (const item of data.data.items) {
        await fetch('/api/cart', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: item.id, quantity: 0 })
        });
      }
    }
    await refreshCart();
  } catch (e) {
    console.error('Failed to clear cart:', e);
  }
}
