'use strict';

// Current view lives in the URL hash (#d=2026-08-14) so back/reload work.

let currentDate = null;
// Set after an edit so the overview skips the server cache.
let upcomingStale = false;
// Sydney date from the server, not the phone's clock.
const todayInSydney = document.body.dataset.today || '';

// Sheet data isn't sanitised on the way in, so always escape.

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function esc(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/[&<>"']/g, ch => ESCAPES[ch]);
}

// Routing

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('hashchange', route);
    route();
});

function route() {
    const match = /^#d=(\d{4}-\d{2}-\d{2})$/.exec(window.location.hash);
    if (match) {
        showDay(match[1]);
    } else {
        showUpcoming();
    }
}

function openDate(date) {
    window.location.hash = 'd=' + date;
}

function showUpcoming() {
    if (window.location.hash) {
        window.location.hash = '';
        return;
    }
    currentDate = null;
    document.getElementById('dayView').hidden = true;
    document.getElementById('overviewView').hidden = false;
    loadUpcoming();
}

function showDay(date) {
    document.getElementById('overviewView').hidden = true;
    document.getElementById('dayView').hidden = false;
    document.getElementById('dayTitle').textContent = describeDate(date);
    if (date !== currentDate) {
        loadDay(date);
    }
}

function jumpToDate() {
    const value = document.getElementById('dateInput').value;
    if (!value) {
        showNotification('Please pick a date', 'error');
        return;
    }
    openDate(value);
}

// Days ahead

async function loadUpcoming(force) {
    const list = document.getElementById('upcomingList');
    if (force) {
        list.innerHTML = '<div class="loading"><div class="spinner"></div>Loading the days ahead...</div>';
    }

    const fresh = force || upcomingStale;

    try {
        const response = await fetch('/staff/api/upcoming' + (fresh ? '?refresh=1' : ''));
        if (response.status === 401 || response.redirected) {
            window.location.href = '/staff';
            return;
        }

        const data = await response.json();
        if (!data.success) {
            list.innerHTML = emptyState('⚠', 'Could not load upcoming bookings',
                data.message || 'Please try again');
            return;
        }

        upcomingStale = false;
        displayUpcoming(data.days || []);
    } catch (error) {
        console.error('Error loading upcoming:', error);
        list.innerHTML = emptyState('⚠', 'Could not load upcoming bookings', 'Please try again');
    }
}

function displayUpcoming(days) {
    const list = document.getElementById('upcomingList');

    if (!days.length) {
        list.innerHTML = emptyState('📅', 'Nothing booked yet',
            'Bookings from today onwards will appear here');
        return;
    }

    list.innerHTML = days.map(day => `
        <button type="button" class="day-row" onclick="openDate('${esc(day.date)}')">
            <span class="day-when">
                <span class="day-weekday">${esc(day.weekday)}</span>
                <span class="day-number">${esc(day.day_month)}</span>
                ${day.relative ? `<span class="day-relative">${esc(day.relative)}</span>` : ''}
            </span>
            <span class="day-counts">
                <span class="day-bookings">${esc(day.bookings)} ${day.bookings === 1 ? 'booking' : 'bookings'}</span>
                ${day.pending ? `<span class="day-pending">${esc(day.pending)} pending</span>` : ''}
                <span class="day-covers">${coversText(day)}</span>
            </span>
            <span class="day-chevron" aria-hidden="true">›</span>
        </button>
    `).join('');
}

// Party size is a bucket, so covers is a range.
function coversText(day) {
    const low = day.covers_low || 0;
    const high = day.covers_high || 0;
    if (!high) return '';
    const span = low === high ? esc(low) : `${esc(low)}–${esc(high)}`;
    return `${span}${day.covers_open ? '+' : ''} covers`;
}

// One date's bookings

async function loadDay(date) {
    if (!date) return;
    currentDate = date;

    const container = document.getElementById('reservationsContainer');
    const statsBar = document.getElementById('statsBar');

    container.innerHTML = '<div class="loading"><div class="spinner"></div>Loading reservations...</div>';
    statsBar.style.display = 'none';

    try {
        const response = await fetch(`/staff/api/reservations/${encodeURIComponent(date)}`);
        if (response.status === 401 || response.redirected) {
            window.location.href = '/staff';
            return;
        }

        const data = await response.json();

        if (data.success && data.reservations.length > 0) {
            displayReservations(data.reservations);
            updateStats(data);
            statsBar.style.display = 'grid';
        } else {
            container.innerHTML = emptyState('📅', 'No reservations found',
                data.message || 'Nothing booked for this date');
        }
    } catch (error) {
        console.error('Error:', error);
        container.innerHTML = emptyState('⚠', 'Error loading reservations', 'Please try again');
    }
}

let bookings = {};

const TIMES = ['12:00', '12:30', '13:00', '13:30', '17:00', '17:30',
    '18:00', '18:30', '19:00', '19:30', '20:00', '20:30'];
const PARTY_SIZES = ['1-2', '3-4', '5-6', '7-10', '10+'];

function displayReservations(reservations) {
    const container = document.getElementById('reservationsContainer');

    bookings = {};
    reservations.forEach(reservation => { bookings[reservation.row_number] = reservation; });

    container.innerHTML = reservations.map(reservation => {
        const status = reservation.confirmed.toLowerCase().trim();

        // Modified = booking moved to another date. Read-only.
        const statusText = status === 'confirmed' || status === 'yes'
            ? 'Confirmed'
            : status === 'cancelled' || status === 'no'
                ? 'Cancelled'
                : status === 'modified'
                    ? 'Modified'
                    : 'Pending';

        const isFinished = statusText === 'Cancelled' || statusText === 'Modified';
        // Can't receive the reminder text, needs a call.
        const needsCall = statusText === 'Pending' && !reservation.textable;
        const badgeText = needsCall ? 'Call to confirm' : statusText;
        const statusClass = needsCall ? 'status-call' : 'status-' + statusText.toLowerCase();

        return `
            <div class="reservation-card ${isFinished ? 'reservation-card--finished' : ''}"
                data-row="${esc(reservation.row_number)}">
                <div class="reservation-header">
                    <div class="customer-name">${esc(reservation.name)}</div>
                    <div class="time-badge">${esc(reservation.time)}</div>
                </div>

                <div class="reservation-details">
                    <div class="detail-row">
                        <span class="detail-label">People</span>
                        <span class="detail-value">${esc(reservation.people)}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Phone</span>
                        <span class="detail-value"><a href="tel:${esc(reservation.phone)}" class="phone-link">${esc(reservation.phone)}</a></span>
                        ${reservation.phone && !reservation.textable
                ? '<span class="no-sms" title="Not an Australian mobile — the reminder text cannot reach it">No SMS</span>'
                : ''}
                    </div>
                    ${reservation.dish_type ? `
                    <div class="detail-row">
                        <span class="detail-label">Dish</span>
                        <span class="detail-value">${esc(reservation.dish_type)}</span>
                    </div>` : ''}
                    ${reservation.reservation_id ? `
                    <div class="detail-row">
                        <span class="detail-label">ID</span>
                        <span class="detail-value">#${esc(reservation.reservation_id)}</span>
                    </div>` : ''}
                </div>

                ${reservation.notes ? `
                <div class="notes-section">
                    <strong>Notes:</strong> ${esc(reservation.notes)}
                </div>` : ''}

                <div class="status-container">
                    <span class="status-badge ${statusClass}">${badgeText}</span>
                    <div class="quick-actions">
                        ${isFinished ? '' : `
                        <button class="action-btn btn-edit" onclick="openEditor(${esc(reservation.row_number)})">
                            Edit
                        </button>`}
                        ${statusText === 'Modified' ? '' : actionButtons(statusText, reservation.row_number)}
                    </div>
                </div>

                <div class="edit-panel" hidden></div>
            </div>
        `;
    }).join('');
}

// Edit panel (inline, not a modal - easier on a phone)

function openEditor(rowNumber) {
    const booking = bookings[rowNumber];
    const card = document.querySelector(`[data-row="${rowNumber}"]`);
    if (!booking || !card) return;

    const panel = card.querySelector('.edit-panel');
    if (!panel.hidden) {  // toggle
        closeEditor(rowNumber);
        return;
    }

    panel.innerHTML = `
        <div class="edit-grid">
            <label class="edit-field">
                <span class="edit-label">Date</span>
                <input type="date" class="edit-input" id="edit-date-${esc(rowNumber)}"
                    value="${esc(currentDate)}" min="${esc(todayInSydney)}">
            </label>
            <label class="edit-field">
                <span class="edit-label">Time</span>
                <select class="edit-input" id="edit-time-${esc(rowNumber)}">
                    ${TIMES.map(time => `
                    <option value="${esc(time)}" ${time === booking.time ? 'selected' : ''}>${esc(time)}</option>`).join('')}
                </select>
            </label>
            <label class="edit-field">
                <span class="edit-label">People</span>
                <select class="edit-input" id="edit-people-${esc(rowNumber)}">
                    ${PARTY_SIZES.map(size => `
                    <option value="${esc(size)}" ${size === booking.people ? 'selected' : ''}>${esc(size)}</option>`).join('')}
                    ${PARTY_SIZES.includes(booking.people) ? '' : `
                    <option value="${esc(booking.people)}" selected>${esc(booking.people)}</option>`}
                </select>
            </label>
            <label class="edit-field">
                <span class="edit-label">Phone</span>
                <input type="tel" class="edit-input" id="edit-phone-${esc(rowNumber)}"
                    value="${esc(booking.phone)}" inputmode="tel">
            </label>
        </div>

        <div class="edit-actions">
            <label class="edit-notify">
                <input type="checkbox" id="edit-notify-${esc(rowNumber)}"
                    ${booking.email ? '' : 'disabled'}>
                <span>${booking.email
            ? 'Email the customer'
            : 'No email on this booking'}</span>
            </label>
            <div class="edit-buttons">
                <button class="action-btn" onclick="closeEditor(${esc(rowNumber)})">Cancel</button>
                <button class="action-btn btn-save" id="edit-save-${esc(rowNumber)}"
                    onclick="saveEdit(${esc(rowNumber)})">Save</button>
            </div>
        </div>
    `;
    panel.hidden = false;
}

function closeEditor(rowNumber) {
    const card = document.querySelector(`[data-row="${rowNumber}"]`);
    if (!card) return;
    const panel = card.querySelector('.edit-panel');
    panel.hidden = true;
    panel.innerHTML = '';
}

async function saveEdit(rowNumber) {
    const booking = bookings[rowNumber];
    if (!booking || !currentDate) return;

    const field = name => document.getElementById(`edit-${name}-${rowNumber}`);
    const saveButton = field('save');
    const date = field('date').value;
    const notify = field('notify').checked;

    // No optimistic update - a date change moves the booking to another tab.
    saveButton.disabled = true;
    saveButton.textContent = 'Saving...';

    try {
        const response = await fetch('/staff/api/update_booking', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                date_tab: currentDate,
                row_number: rowNumber,
                reservation_id: booking.reservation_id,
                date: date,
                time: field('time').value,
                people: field('people').value,
                phone: field('phone').value,
                // server rejects the save if these no longer match
                expect: {
                    time: booking.time,
                    people: booking.people,
                    phone: booking.phone
                },
                notify: notify
            })
        });

        if (response.status === 401 || response.redirected) {
            window.location.href = '/staff';
            return;
        }

        const result = await response.json();

        if (!result.success) {
            showNotification(result.message || 'Could not save that change', 'error');
            saveButton.disabled = false;
            saveButton.textContent = 'Save';
            // Someone else changed it, reload.
            if (result.stale) {
                upcomingStale = true;
                loadDay(currentDate);
            }
            return;
        }

        upcomingStale = true;
        showNotification(result.message, 'success');
        (result.warnings || []).forEach(warning => showNotification(warning, 'warn'));
        loadDay(currentDate);
    } catch (error) {
        console.error('Error saving booking:', error);
        showNotification('Could not save that change', 'error');
        saveButton.disabled = false;
        saveButton.textContent = 'Save';
    }
}

function actionButtons(statusText, rowNumber) {
    const row = esc(rowNumber);
    return `
        ${statusText !== 'Confirmed' ? `
        <button class="action-btn btn-confirm" onclick="updateStatus(${row}, 'Confirmed')">
            Confirm
        </button>` : ''}
        ${statusText !== 'Cancelled' ? `
        <button class="action-btn btn-cancel" onclick="updateStatus(${row}, 'Cancelled')">
            Cancel
        </button>` : ''}
    `;
}

function updateStats(data) {
    document.getElementById('totalReservations').textContent = data.reservations.length;
    document.getElementById('confirmedCount').textContent = data.total_confirmed;
    document.getElementById('pendingCount').textContent = data.total_pending;
    const low = data.covers_low || 0;
    const high = data.covers_high || 0;
    document.getElementById('coversCount').textContent =
        (low === high ? String(low) : `${low}–${high}`) + (data.covers_open ? '+' : '');
}

async function updateStatus(rowNumber, newStatus) {
    const card = document.querySelector(`[data-row="${rowNumber}"]`);
    if (!card || !currentDate) return;

    const date = currentDate;
    const statusBadge = card.querySelector('.status-badge');
    const quickActions = card.querySelector('.quick-actions');

    statusBadge.textContent = newStatus;
    statusBadge.className = `status-badge status-${newStatus.toLowerCase()}`;
    quickActions.innerHTML = actionButtons(newStatus, rowNumber);

    showNotification(`Reservation ${newStatus.toLowerCase()}`, 'success');
    upcomingStale = true;

    try {
        const response = await fetch('/staff/api/update_status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                date,
                row_number: rowNumber,
                reservation_id: (bookings[rowNumber] || {}).reservation_id,
                status: newStatus
            })
        });

        if (response.status === 401 || response.redirected) {
            window.location.href = '/staff';
            return;
        }

        const result = await response.json();
        if (!result.success) {
            loadDay(date);
            showNotification(result.message || 'Error updating reservation', 'error');
        }
    } catch (error) {
        console.error('Error updating status:', error);
        loadDay(date);
        showNotification('Error updating reservation', 'error');
    }
}

// Helpers

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];

// Not new Date('2026-08-14') - that parses as UTC.
function describeDate(iso) {
    const [year, month, day] = iso.split('-').map(Number);
    if (!year || !month || !day) return iso;
    const weekday = WEEKDAYS[new Date(year, month - 1, day).getDay()];
    return `${weekday}, ${day} ${MONTHS[month - 1]} ${year}`;
}

function emptyState(icon, heading, detail) {
    return `
        <div class="empty-state">
            <div class="empty-icon">${esc(icon)}</div>
            <h3>${esc(heading)}</h3>
            <p>${esc(detail)}</p>
        </div>
    `;
}

// Toasts stack so multiple messages don't overlap.
function showNotification(message, type) {
    let stack = document.getElementById('toastStack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toastStack';
        stack.className = 'toast-stack';
        document.body.appendChild(stack);
    }

    const toast = document.createElement('div');
    toast.className = `toast toast--${type === 'success' ? 'success' : type === 'warn' ? 'warn' : 'error'}`;
    toast.textContent = message;
    stack.appendChild(toast);

    setTimeout(() => toast.remove(), type === 'success' ? 3000 : 7000);
}
