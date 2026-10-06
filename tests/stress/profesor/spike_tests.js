// Basado en el spike_tests.js de la cátedra (original en ./original/).
// Cambios: la URL, la carpeta de PDFs y el timeout se leen del entorno.
// Sin variables, el timeout queda en 60s, el valor por defecto de k6, igual que el original.
import http from 'k6/http';
import { Trend } from 'k6/metrics';
import { check } from 'k6';

const statusTrend = new Trend('status_codes');

export const options = {
    stages: [
        { duration: '10s', target: 100 },
        { duration: '20s', target: 100 },
        { duration: '10s', target: 0 },
    ],
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8001';
const PDF_DIR = __ENV.PDF_DIR || '../pdfs';
const TIMEOUT = __ENV.TIMEOUT || '60s';

// Carga de PDFs en modo binario durante la inicialización (init context de k6)
const pdfFiles = [
    open(`${PDF_DIR}/2020-Scrum-Guide-Spanish-Latin-South-American.pdf`, 'b'),
    open(`${PDF_DIR}/Essential-Kanban-Condensed-Spanish.pdf`, 'b'),
    open(`${PDF_DIR}/Filosofia_Lean.pdf`, 'b'),
    open(`${PDF_DIR}/scrum_manager_historias_usuario.pdf`, 'b'),
];

export default function () {
    // Selección aleatoria de un PDF de la lista
    const randomPdf = pdfFiles[Math.floor(Math.random() * pdfFiles.length)];

    const params = {
        headers: {
            'Content-Type': 'application/pdf',
        },
        timeout: TIMEOUT,
    };

    const res = http.post(`${BASE_URL}/extract`, randomPdf, params);

    statusTrend.add(res.status);

    check(res, {
        'status 200': (r) => r.status === 200,
    });
}