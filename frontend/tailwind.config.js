/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#fdf8f0',
          100: '#f9eddb',
          200: '#f2d7b0',
          300: '#e9bc7d',
          400: '#e09d4e',
          500: '#d4852e',
          600: '#c56d23',
          700: '#a45420',
          800: '#844321',
          900: '#6b381e',
          950: '#3a1c0e',
        },
        warm: {
          50: '#fefcf9',
          100: '#fdf6ed',
          200: '#f9e9d3',
          300: '#f3d7b0',
          400: '#ebbc82',
          500: '#e19d58',
          600: '#d8823a',
          700: '#b4672d',
          800: '#90532a',
          900: '#754626',
        },
      },
      fontFamily: {
        sans: ['"Noto Sans SC"', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
