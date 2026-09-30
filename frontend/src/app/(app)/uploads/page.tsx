import { PhasePlaceholder } from "@/components/PhasePlaceholder";

export default function UploadsPage() {
  return (
    <PhasePlaceholder
      eyebrow="Ferrocrete Builders, Inc."
      title="Uploads"
      meta="The approved-but-not-uploaded queue, reviewed before starting a Chrome session"
      phase="Arrives in Phase 3"
      summary="This app never drives the browser. Linda opens a chat in the BuilderTrend upload project with Chrome already signed in, and the session reads approved invoices from the API. This screen is what she checks first, and where the last session's results are reported."
      willDo={[
        "List every invoice sitting in approved, with its cost code rows exactly as they will be entered.",
        "Show the last session's results: what saved, what was skipped, and why.",
        "Surface the per-project quirks the session needs to know before it starts.",
      ]}
    />
  );
}
