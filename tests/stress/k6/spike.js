// Test Spike con k6 (modelo cerrado): cada VU envía un PDF, espera la respuesta y
// envía el siguiente, así que la carga la marca la cantidad de VUs y no un ritmo fijo.
//
// Perfil: 0 -> 100 VUs en 10s, 100 VUs durante 20s, 100 -> 0 en 10s.
// Uso y variables de entorno: ver tests/stress/README.md

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8001';
const PDF_DIR = __ENV.PDF_DIR || '../pdfs';
const PDF_FILES = (__ENV.PDF_FILES || '01.pdf,02.pdf,03.pdf,04.pdf').split(',');
const TIMEOUT = __ENV.TIMEOUT || '30s';
const SUMMARY_JSON = __ENV.SUMMARY_JSON;

// open() solo se puede usar en el init context: los PDFs se cargan una vez por VU.
const pdfs = PDF_FILES.map((name) => ({ name, data: open(`${PDF_DIR}/${name}`, 'b') }));

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '10s', target: 100 },
        { duration: '20s', target: 100 },
        { duration: '10s', target: 0 },
      ],
      gracefulRampDown: '30s',
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'max'],
};

export default function () {
  // Rota los PDFs; sumar __VU evita que todos los VUs manden el mismo archivo a la vez.
  const pdf = pdfs[(__VU + __ITER) % pdfs.length];
  const res = http.post(
    `${BASE_URL}/extract`,
    { file: http.file(pdf.data, pdf.name, 'application/pdf') },
    { timeout: TIMEOUT, tags: { pdf: pdf.name } },
  );
  check(res, { 'status 200': (r) => r.status === 200 });
}

export function handleSummary(data) {
  const reqs = data.metrics.http_reqs.values;
  const failed = data.metrics.http_req_failed.values;
  const duration = data.metrics.http_req_duration.values;
  const ms = (value) => `${value.toFixed(0)} ms`;

  const report = [
    '',
    '== Spike (k6, modelo cerrado) ==',
    `Solicitudes:     ${reqs.count}`,
    `Throughput:      ${reqs.rate.toFixed(2)} req/s`,
    `Tasa de error:   ${(failed.rate * 100).toFixed(2)} %`,
    'Latencias:',
    `  mediana        ${ms(duration.med)}`,
    `  P90            ${ms(duration['p(90)'])}`,
    `  P95            ${ms(duration['p(95)'])}`,
    `  máxima         ${ms(duration.max)}`,
    '',
  ].join('\n');

  const outputs = { stdout: report };
  if (SUMMARY_JSON) {
    outputs[SUMMARY_JSON] = JSON.stringify(data, null, 2);
  }
  return outputs;
}
