export async function GET() {
  return Response.json(
    {
      status: "ok",
      service: "zixu-web",
    },
    {
      status: 200,
      headers: { "Cache-Control": "no-store" },
    },
  );
}
