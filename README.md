# Mission Control ULTRA (Next.js + Tailwind)

A premium, polished dark UI portfolio for Mohammad Machaka — **Mission Debrief → Projects → Contact** — with glass surfaces, gradient borders, spotlight cursor, animated starfield, and refined typography (Inter + Space Grotesk + DM Mono).

## Run locally
```bash
npm install
npm run dev
```
Open the localhost URL.

## Edit content
- `data/missions.ts` — missions/operations copy & metrics
- `app/page.tsx` — hero copy & section order
- Replace `public/cv.pdf` with your real CV

## Deploy
- **AWS Amplify** (recommended) or **S3 + CloudFront** for static assets.
- This is an SSR-ready Next.js app; Amplify is simplest for CI/CD.

## Notes
- Respects `prefers-reduced-motion` for starfield animation.
- Design tokens live in `tailwind.config.ts` (colors, radius, shadows).