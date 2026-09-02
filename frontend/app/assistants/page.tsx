import { AssistantWorkspace } from "@/components/assistants/assistant-workspace";


interface AssistantsPageProps {
  searchParams: Promise<{
    session?: string | string[];
    plugin?: string | string[];
  }>;
}

export default async function AssistantsPage({ searchParams }: AssistantsPageProps) {
  const params = await searchParams;
  return (
    <AssistantWorkspace
      initialSessionId={typeof params.session === "string" ? params.session : null}
      initialPluginId={typeof params.plugin === "string" ? params.plugin : null}
    />
  );
}
