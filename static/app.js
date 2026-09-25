document.addEventListener('DOMContentLoaded', () => {
    const sshForm = document.getElementById('ssh-form');
    const submitBtn = document.getElementById('btn-submit');
    const stateCard = document.getElementById('state-card');
    const stateTitle = document.getElementById('state-title');
    const stateDesc = document.getElementById('state-desc');
    const loader = document.getElementById('loader');
    
    // Telemetry Elements
    const telemetrySection = document.getElementById('telemetry-section');
    const telHostname = document.getElementById('tel-hostname');
    const telUptime = document.getElementById('tel-uptime');
    const telKernel = document.getElementById('tel-kernel');
    const telMem = document.getElementById('tel-mem');
    
    // Gauges Elements
    const gaugesSection = document.getElementById('gauges-section');
    const diskGaugeCircle = document.getElementById('disk-gauge-circle');
    const diskPercentageText = document.getElementById('disk-percentage-text');
    const lblTotal = document.getElementById('lbl-total');
    const lblUsed = document.getElementById('lbl-used');
    const lblAvail = document.getElementById('lbl-avail');
    
    // Table Elements
    const tableSection = document.getElementById('table-section');
    const diskTableBody = document.getElementById('disk-table-body');

    sshForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const host = document.getElementById('ssh-host').value;
        const username = document.getElementById('ssh-user').value;
        const port = document.getElementById('ssh-port').value;
        const password = document.getElementById('ssh-pass').value;

        // Transition to loading state
        stateCard.classList.remove('hidden');
        stateTitle.innerText = "Connessione in corso...";
        stateDesc.innerText = `Tentativo di connessione a SSH su ${host}:${port}...`;
        loader.classList.remove('hidden');
        
        // Hide previous results
        telemetrySection.classList.add('hidden');
        gaugesSection.classList.add('hidden');
        tableSection.classList.add('hidden');
        
        submitBtn.disabled = true;

        try {
            const response = await fetch('/api/check_disk', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ host, username, port, password })
            });
            
            const result = await response.json();
            
            if (result.success) {
                // Connection Succeeded! Hide empty state / loading card
                stateCard.classList.add('hidden');
                
                // Populate Telemetry
                telHostname.textContent = result.telemetry.hostname || 'Unknown';
                telUptime.textContent = result.telemetry.uptime || 'Unknown';
                telKernel.textContent = result.telemetry.kernel || 'Unknown';
                telMem.textContent = `${result.telemetry.mem_free || '-'} / ${result.telemetry.mem_total || '-'}`;
                telemetrySection.classList.remove('hidden');
                
                // Find Root Partition / (or the primary storage mapped)
                const rootDisk = result.disks.find(d => d.mounted_on === '/') || result.disks[0];
                if (rootDisk) {
                    lblTotal.textContent = rootDisk.size;
                    lblUsed.textContent = rootDisk.used;
                    lblAvail.textContent = rootDisk.avail;
                    
                    const pct = rootDisk.use_percent;
                    diskPercentageText.textContent = `${pct}%`;
                    
                    // Radial progress circle logic (radius=50, circumference ~ 314.16)
                    const circ = 2 * Math.PI * 50;
                    const offset = circ - (pct / 100) * circ;
                    diskGaugeCircle.style.strokeDashoffset = offset;
                    
                    // Color-code gauge circle based on safety thresholds
                    diskGaugeCircle.classList.remove('color-safe', 'color-warning', 'color-danger');
                    if (pct < 70) {
                        diskGaugeCircle.style.stroke = '#10B981'; // Green
                    } else if (pct < 85) {
                        diskGaugeCircle.style.stroke = '#F59E0B'; // Orange
                    } else {
                        diskGaugeCircle.style.stroke = '#EF4444'; // Red
                    }
                    
                    gaugesSection.classList.remove('hidden');
                }
                
                // Populate Table
                diskTableBody.replaceChildren();
                result.disks.forEach(disk => {
                    const row = document.createElement('tr');
                    
                    let colorClass = 'color-safe';
                    if (disk.use_percent >= 85) {
                        colorClass = 'color-danger';
                    } else if (disk.use_percent >= 70) {
                        colorClass = 'color-warning';
                    }
                    
                    const addTextCell = (value, styles = {}) => {
                        const cell = document.createElement('td');
                        Object.assign(cell.style, styles);
                        cell.textContent = value == null ? '' : String(value);
                        row.appendChild(cell);
                        return cell;
                    };
                    addTextCell(disk.filesystem, {fontWeight: '600', color: '#fff'});
                    addTextCell(disk.size);
                    addTextCell(disk.used);
                    addTextCell(disk.avail);

                    const usageCell = document.createElement('td');
                    const progressContainer = document.createElement('div');
                    progressContainer.className = 'progress-bar-container';
                    const percentage = document.createElement('span');
                    percentage.style.minWidth = '35px';
                    percentage.style.textAlign = 'right';
                    percentage.style.fontWeight = '600';
                    const usePercent = Math.max(0, Math.min(100, Number(disk.use_percent) || 0));
                    percentage.textContent = `${usePercent}%`;
                    const progressBg = document.createElement('div');
                    progressBg.className = 'progress-bar-bg';
                    const progressFg = document.createElement('div');
                    progressFg.className = `progress-bar-fg ${colorClass}`;
                    progressFg.style.width = `${usePercent}%`;
                    progressBg.appendChild(progressFg);
                    progressContainer.appendChild(percentage);
                    progressContainer.appendChild(progressBg);
                    usageCell.appendChild(progressContainer);
                    row.appendChild(usageCell);

                    addTextCell(disk.mounted_on, {color: 'var(--primary)', fontWeight: '600'});
                    diskTableBody.appendChild(row);
                });
                
                tableSection.classList.remove('hidden');
                
            } else {
                // Connection failed
                showErrorState(result.error || 'Errore di connessione SSH non specificato.');
            }
            
        } catch (err) {
            showErrorState(`Impossibile comunicare con il server locale: ${err.message}`);
        } finally {
            submitBtn.disabled = false;
        }
    });
    
    function showErrorState(message) {
        stateCard.classList.remove('hidden');
        stateTitle.innerText = "Connessione Fallita ❌";
        stateDesc.innerText = message;
        loader.classList.add('hidden');
    }
});
