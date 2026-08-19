/**
 * Capture, and the wait afterwards.
 *
 * On a phone the camera is the primary path — `capture="environment"` opens
 * it directly rather than a file picker. Choosing a file is a secondary text
 * link, not a peer button.
 *
 * Extraction genuinely takes about thirty seconds, so the wait shows named
 * pipeline stages rather than an indeterminate bar. "Checked the arithmetic"
 * tells you where it is; a spinner does not.
 */

import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { Button, Card, ErrorNote } from "../components/ui";

type Stage = { key: string; label: string };

const STAGES: Stage[] = [
  { key: "upload", label: "Stored" },
  { key: "extract", label: "Transcribed" },
  { key: "normalize", label: "Read the line items" },
  { key: "reconcile", label: "Checked the arithmetic" },
  { key: "resolve", label: "Identified the foods" },
  { key: "derive", label: "Worked out the prices" },
];

export function Scan({ onDone }: { onDone: (receiptId: number) => void }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [done, setDone] = useState<string[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const running = active !== null;

  async function run(file: File) {
    setError(null);
    setNote(null);
    setDone([]);

    const step = async (key: string, work: () => Promise<void>) => {
      setActive(key);
      await work();
      setDone((previous) => [...previous, key]);
    };

    try {
      let receiptId = 0;
      await step("upload", async () => {
        const result = await api.upload(file);
        receiptId = result.receipt.id;
        if (!result.created) {
          // Idempotent on image content. Say so plainly rather than implying
          // a second receipt was recorded.
          setNote("You've uploaded this photo before — opening the receipt you already have.");
        }
      });

      await step("extract", () => api.extract(receiptId).then(() => undefined));
      await step("normalize", () => api.normalize(receiptId).then(() => undefined));
      await step("reconcile", () => api.reconcile(receiptId).then(() => undefined));
      await step("resolve", () => api.resolve(receiptId).then(() => undefined));
      await step("derive", () => api.derive(receiptId).then(() => undefined));

      setActive(null);
      onDone(receiptId);
    } catch (caught) {
      setActive(null);
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Something went wrong. Your receipt is saved — try again.",
      );
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-title font-semibold tracking-[-0.01em]">Add a receipt</h1>
        <p className="max-w-[65ch] text-body text-ink-2">
          One photo of the whole receipt. It's stored before anything else
          happens, so nothing is lost if the rest fails.
        </p>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {!running && (
        <>
          <div className="flex flex-col items-center gap-4 rounded-card border border-line bg-surface p-6">
            <div className="flex aspect-[3/4] w-full max-w-[320px] items-center justify-center rounded-[6px] border border-dashed border-line-strong bg-sunken p-6">
              <p className="max-w-[26ch] text-center text-label text-ink-2">
                Fit the whole receipt in frame. A long one can be photographed
                in two goes.
              </p>
            </div>

            <input
              ref={fileInput}
              type="file"
              accept="image/*"
              capture="environment"
              className="sr-only"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void run(file);
                event.target.value = "";
              }}
            />

            <Button variant="primary" full onClick={() => fileInput.current?.click()}>
              Take a photo
            </Button>
            <Button variant="text" onClick={() => fileInput.current?.click()}>
              Choose a file instead
            </Button>
          </div>
          {note && <p className="text-label text-ink-2">{note}</p>}
        </>
      )}

      {running && (
        <Card>
          <p className="text-label font-medium text-ink-2">Reading your receipt</p>
          <ul className="flex flex-col">
            {STAGES.map((stage) => {
              const complete = done.includes(stage.key);
              const current = active === stage.key;
              return (
                <li
                  key={stage.key}
                  className="flex items-center justify-between gap-3 border-b border-line py-[10px] last:border-b-0"
                >
                  <span className={complete || current ? "text-ink" : "text-ink-3"}>
                    {stage.label}
                  </span>
                  <span className="text-caption text-ink-2">
                    {complete ? "done" : current ? "working" : "waiting"}
                  </span>
                </li>
              );
            })}
          </ul>
          <p className="text-caption text-ink-2">
            Transcribing takes about 30 seconds. You can leave this screen.
          </p>
        </Card>
      )}
    </div>
  );
}
