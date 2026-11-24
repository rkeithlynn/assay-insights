/**
 * Where tenant roles land until Ask exists (build step 7; owner decision D7). Nothing here
 * reads tenant content.
 */

import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useNavigate } from "react-router";

import { useSession } from "../session";

export function Home() {
  const { me } = useSession();
  const navigate = useNavigate();
  return (
    <ContentLayout header={<Header variant="h1">Welcome, {me.user.name}</Header>}>
      <Container header={<Header variant="h2">{me.tenant.name}</Header>}>
        <SpaceBetween size="s">
          <Box>Asking questions about your documents, uploads and ingestion history arrive in a later release.</Box>
          {me.role === "tenant-admin" && (
            <Box>
              You can already{" "}
              <Link
                href="/users"
                onFollow={(event) => {
                  event.preventDefault();
                  void navigate("/users");
                }}
              >
                invite and manage your organization&apos;s users
              </Link>
              .
            </Box>
          )}
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}
