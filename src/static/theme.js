// src/static/theme.js

document.addEventListener('DOMContentLoaded', () => {
    const themeToggleButton = document.getElementById('theme-toggle-btn');

    // Function to apply the theme
    const applyTheme = (theme) => {
        if (theme === 'dark') {
            document.body.classList.add('dark-mode');
        } else {
            document.body.classList.remove('dark-mode');
        }
        // Update the icon based on the current theme
        updateToggleIcon(theme);
    };

    // Function to update the toggle button's icon
    const updateToggleIcon = (theme) => {
        if (themeToggleButton) {
            const icon = themeToggleButton.querySelector('i');
            if (theme === 'dark') {
                icon.classList.remove('fa-moon');
                icon.classList.add('fa-sun');
            } else {
                icon.classList.remove('fa-sun');
                icon.classList.add('fa-moon');
            }
        }
    };

    // Event listener for the toggle button
    if (themeToggleButton) {
        themeToggleButton.addEventListener('click', () => {
            let newTheme = 'light';
            if (!document.body.classList.contains('dark-mode')) {
                newTheme = 'dark';
            }
            // Save the new theme to localStorage and apply it
            localStorage.setItem('theme', newTheme);
            applyTheme(newTheme);
        });
    }

    // On page load, check for a saved theme in localStorage
    const savedTheme = localStorage.getItem('theme') || 'light';
    applyTheme(savedTheme);
});