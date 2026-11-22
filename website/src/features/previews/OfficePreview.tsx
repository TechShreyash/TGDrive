import { NativeSelect, Stack, Table, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";
import { errorMessage, request } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import { fileUrl } from "../../shared/types";
import "./OfficePreview.css";

type OfficeDocument = {
    kind: "document";
    paragraphs: { text: string; heading: boolean }[];
    truncated: boolean;
};

type Worksheet = {
    kind: "spreadsheet";
    sheets: string[];
    selected: number;
    rows: string[][];
    truncated: boolean;
};

export default function OfficePreview({
    path,
    auth,
}: {
    path: string;
    auth: string;
}) {
    const [sheet, setSheet] = useState("0");
    const [data, setData] = useState<OfficeDocument | Worksheet | null>(null);
    const [error, setError] = useState("");
    useEffect(() => {
        const controller = new AbortController();
        const params = new URLSearchParams({ path });
        if (auth) params.set("auth", auth);
        if (sheet !== "0") params.set("entry", sheet);
        void request<OfficeDocument | Worksheet>(
            "/api/v1/books?" + params,
            undefined,
            { signal: controller.signal },
        )
            .then((result) => {
                if (!controller.signal.aborted) setData(result);
            })
            .catch((cause) => {
                if (!controller.signal.aborted) setError(errorMessage(cause));
            });
        return () => controller.abort();
    }, [path, auth, sheet]);

    const ready =
        data && (data.kind === "document" || String(data.selected) === sheet);
    return (
        <Stack className="office-preview" p="md" w="100%" maw={1000} mx="auto">
            {data?.kind === "spreadsheet" && (
                <div className="office-sheet-picker">
                    <NativeSelect
                        label="Worksheet"
                        value={sheet}
                        onChange={(event) => {
                            setError("");
                            setSheet(event.currentTarget.value);
                        }}
                        data={data.sheets.map((label, index) => ({
                            label,
                            value: String(index),
                        }))}
                        maw={320}
                    />
                </div>
            )}
            {error ? (
                <>
                    <ErrorNotice message={error} />
                    <a className="inline-link" href={fileUrl(path, auth, true)}>
                        Download original
                    </a>
                </>
            ) : !ready ? (
                <div role="status">
                    <Spinner /> Reading Office preview…
                </div>
            ) : (
                <>
                    {data.truncated && (
                        <Text c="dimmed" size="sm">
                            Showing part of this file. Download the original to
                            see all the content.
                        </Text>
                    )}
                    {data.kind === "document" ? (
                        data.paragraphs.length ? (
                            <Stack
                                component="article"
                                className="office-document"
                                gap="sm"
                            >
                                {data.paragraphs.map(
                                    ({ heading, text }, index) =>
                                        heading ? (
                                            <Title key={index} order={3}>
                                                {text}
                                            </Title>
                                        ) : (
                                            <Text key={index} component="p">
                                                {text}
                                            </Text>
                                        ),
                                )}
                            </Stack>
                        ) : (
                            <Text>This document has no text to preview.</Text>
                        )
                    ) : data.rows.length ? (
                        <Table.ScrollContainer
                            className="office-sheet-scroll"
                            minWidth={44 + data.rows[0].length * 144}
                            type="native"
                            tabIndex={0}
                            role="region"
                            aria-label={
                                data.sheets[data.selected] + " worksheet cells"
                            }
                        >
                            <Table
                                className="office-sheet"
                                layout="fixed"
                                captionSide="top"
                                striped
                                withColumnBorders
                            >
                                <colgroup>
                                    <col className="office-sheet-row-number" />
                                </colgroup>
                                <Table.Caption>
                                    {data.sheets[data.selected]}
                                </Table.Caption>
                                <Table.Thead>
                                    <Table.Tr>
                                        <Table.Th scope="col">Row</Table.Th>
                                        {data.rows[0].map((_, index) => (
                                            <Table.Th key={index} scope="col">
                                                {String.fromCharCode(
                                                    65 + index,
                                                )}
                                            </Table.Th>
                                        ))}
                                    </Table.Tr>
                                </Table.Thead>
                                <Table.Tbody>
                                    {data.rows.map((row, index) => (
                                        <Table.Tr key={index}>
                                            <Table.Th scope="row">
                                                {index + 1}
                                            </Table.Th>
                                            {row.map((value, column) => (
                                                <Table.Td key={column}>
                                                    {value}
                                                </Table.Td>
                                            ))}
                                        </Table.Tr>
                                    ))}
                                </Table.Tbody>
                            </Table>
                        </Table.ScrollContainer>
                    ) : (
                        <Text>This worksheet is empty.</Text>
                    )}
                </>
            )}
        </Stack>
    );
}
