async function loadReservations() {
    const dateInput = document.getElementById('dateInput');
    const container = document.getElementById('reservationsContainer');
    const statsBar = document.getElementById('statsBar');

    if (!dateInput.value) {
        alert('Please select a date');
        return;
    }

    container.innerHTML = '<div class="loading"><div class="spinner"></div>Loading reservations...</div>';
    statsBar.style.display = 'none';

    try {
        const response = await fetch(`/staff/api/reservations/${dateInput.value}`);

        if (response.status === 401) {
            window.location.href = '/staff';
            return;
        }

        const data = await response.json();

        if (data.success && data.reservations.length > 0) {
            displayReservations(data.reservations);
            updateStats(data);
            statsBar.style.display = 'grid';
        } else {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">📅</div>
                    <h3>No reservations found</h3>
                    <p>${data.message}</p>
                </div>
            `;
        }
    } catch (error) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">⚠</div>
                <h3>Error loading reservations</h3>
                <p>Please try again</p>
            </div>
        `;
        console.error('Error:', error);
    }
}

function displayReservations(reservations) {
    const container = document.getElementById('reservationsContainer');

    container.innerHTML = reservations.map(reservation => {
        const statusClass = reservation.confirmed.toLowerCase() === 'confirmed' || reservation.confirmed.toLowerCase() === 'yes'
            ? 'status-confirmed'
            : reservation.confirmed.toLowerCase() === 'cancelled' || reservation.confirmed.toLowerCase() === 'no'
                ? 'status-cancelled'
                : 'status-pending';

        const statusText = reservation.confirmed.toLowerCase() === 'confirmed' || reservation.confirmed.toLowerCase() === 'yes'
            ? 'Confirmed'
            : reservation.confirmed.toLowerCase() === 'cancelled' || reservation.confirmed.toLowerCase() === 'no'
                ? 'Cancelled'
                : 'Pending';

        return `
            <div class="reservation-card"
                data-row="${reservation.row_number}"
                data-date="${reservation.date}">
                <div class="reservation-header">
                    <div class="customer-name">${reservation.name}</div>
                    <div class="time-badge">${reservation.time}</div>
                </div>

                <div class="reservation-details">
                    <div class="detail-row">
                        <span class="detail-label">People</span>
                        <span class="detail-value">${reservation.people}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Phone</span>
                        <span class="detail-value"><a href="tel:${reservation.phone}" class="phone-link">${reservation.phone}</a></span>
                    </div>
                    ${reservation.dish_type ? `
                    <div class="detail-row">
                        <span class="detail-label">Dish</span>
                        <span class="detail-value">${reservation.dish_type}</span>
                    </div>` : ''}
                    ${reservation.reservation_id ? `
                    <div class="detail-row">
                        <span class="detail-label">ID</span>
                        <span class="detail-value">#${reservation.reservation_id}</span>
                    </div>` : ''}
                </div>

                ${reservation.notes ? `
                <div class="notes-section">
                    <strong>Notes:</strong> ${reservation.notes}
                </div>` : ''}

                <div class="status-container">
                    <span class="status-badge ${statusClass}">${statusText}</span>
                    <div class="quick-actions">
                        ${statusText !== 'Confirmed' ? `
                        <button class="action-btn btn-confirm" onclick="updateStatus('${reservation.date}', ${reservation.row_number}, 'Confirmed')">
                            Confirm
                        </button>` : ''}
                        ${statusText !== 'Cancelled' ? `
                        <button class="action-btn btn-cancel" onclick="updateStatus('${reservation.date}', ${reservation.row_number}, 'Cancelled')">
                            Cancel
                        </button>` : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function updateStats(data) {
    document.getElementById('totalReservations').textContent = data.reservations.length;
    document.getElementById('confirmedCount').textContent = data.total_confirmed;
    document.getElementById('pendingCount').textContent = data.total_pending;
}

async function updateStatus(date, rowNumber, newStatus) {
    const card = document.querySelector(`[data-row="${rowNumber}"]`);
    if (!card) return;

    const statusBadge = card.querySelector('.status-badge');
    const quickActions = card.querySelector('.quick-actions');

    statusBadge.textContent = newStatus;
    statusBadge.className = `status-badge status-${newStatus.toLowerCase()}`;

    quickActions.innerHTML = `
        ${newStatus !== 'Confirmed' ? `
        <button class="action-btn btn-confirm" onclick="updateStatus('${date}', ${rowNumber}, 'Confirmed')">
            Confirm
        </button>` : ''}
        ${newStatus !== 'Cancelled' ? `
        <button class="action-btn btn-cancel" onclick="updateStatus('${date}', ${rowNumber}, 'Cancelled')">
            Cancel
        </button>` : ''}
    `;

    showNotification(`Reservation ${newStatus.toLowerCase()}`, 'success');

    try {
        const response = await fetch('/staff/api/update_status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date, row_number: rowNumber, status: newStatus })
        });

        if (response.status === 401) {
            window.location.href = '/staff';
            return;
        }

        const result = await response.json();
        if (!result.success) {
            loadReservations();
            showNotification('Error updating reservation', 'error');
        }
    } catch (error) {
        console.error('Error updating status:', error);
        loadReservations();
        showNotification('Error updating reservation', 'error');
    }
}

function showNotification(message, type) {
    const notification = document.createElement('div');
    notification.style.cssText = `
        position: fixed;
        bottom: 24px;
        right: 24px;
        padding: 12px 20px;
        border-radius: 8px;
        color: white;
        font-size: 14px;
        font-weight: 500;
        z-index: 1000;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        ${type === 'success' ? 'background: #16a34a;' : 'background: #dc2626;'}
    `;
    notification.textContent = message;
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 3000);
}
