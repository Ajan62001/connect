"use client";

import { PageHeader } from "@/components/shared/PageHeader";
import { ContentStudio } from "@/components/content/ContentStudio";

export default function ContentPage() {
  return (
    <>
      <PageHeader
        title="Content"
        description="Generate grounded social posts and reels from your corpus — control the voice, script and images, then review and publish."
      />
      <ContentStudio />
    </>
  );
}
